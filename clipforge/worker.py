from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import shutil
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from pydantic import ValidationError

from . import __version__
from .brain import (LocalLLM, ReviewRequired, generate_metadata, prompt, select_clips,
                    speech_model_digest, transcribe)
from .common import (DB, Metadata, Settings, SourceManifest, atomic_json, iso_now,
                     load_settings, run_command, sha256_file, stable_id, utcnow)
from .job_control import JobCancelled, checkpoint, report

log = logging.getLogger("clipforge")


def probe_source(settings: Settings, path: Path) -> float:
    result = json.loads(run_command([settings.render.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format",
                                    "-show_streams", "-of", "json", str(path)]).stdout)
    if not any(s.get("codec_type") == "video" for s in result["streams"]):
        raise ReviewRequired("no video stream")
    if not any(s.get("codec_type") == "audio" for s in result["streams"]):
        raise ReviewRequired("no audio stream; supply an authorized narrated source")
    return float(result["format"]["duration"])


def append_credits(metadata: Metadata, manifest: SourceManifest) -> Metadata:
    # These strings are operator-verified records; never let a model rewrite them.
    description = metadata.description + "\n\n" + manifest.creator_credit
    if manifest.music_attribution:
        description += "\n" + manifest.music_attribution
    return Metadata(title=metadata.title, description=description, tags=metadata.tags)


def select_music(settings: Settings, manifest: SourceManifest, source_sha: str) -> SourceManifest:
    if manifest.music_path or not manifest.music_catalog:
        return manifest
    catalog = json.loads(settings.resolve(manifest.music_catalog).read_text(encoding="utf-8"))
    approved = [m for m in catalog["tracks"] if m.get("approved") is True and
                m.get("license_reference") and m.get("path") and "attribution" in m]
    if not approved:
        raise ReviewRequired("music catalog contains no verified approved track")
    track = sorted(approved, key=lambda x: x["path"])[int(source_sha[:12], 16) % len(approved)]
    if not settings.resolve(track["path"]).is_file():
        raise ReviewRequired("selected catalog track missing")
    return manifest.model_copy(update={"music_path": track["path"],
                                       "music_license_reference": track["license_reference"],
                                       "music_attribution": track["attribution"]})


def prepare_manifest(settings: Settings, db: DB, path: Path, retry: bool = False) -> dict:
    stage = "preparing"
    folder = None
    new_folder = False
    job_id = stable_id(str(path.resolve()))
    db.conn.execute("INSERT OR IGNORE INTO jobs(id,manifest_path,state,updated_at) VALUES(?,?,?,?)",
                    (job_id, str(path.resolve()), "NEW", iso_now()))
    try:
        report("preparing")
        manifest = SourceManifest.model_validate_json(path.read_text(encoding="utf-8"))
        if manifest.schedule:
            from .upload import validate_schedule
            try:
                validate_schedule(manifest.schedule, settings.upload.min_lead_minutes)
            except ValueError as error:
                raise ReviewRequired(str(error)) from error
        media = settings.resolve(manifest.media_path)
        if not media.is_file():
            db.set_job(job_id, "WAITING_MEDIA", "authorized original file not present")
            return {"job_id": job_id, "state": "WAITING_MEDIA"}
        source_sha = sha256_file(media)
        manifest = select_music(settings, manifest, source_sha)
        supplied_hashes = {}
        for field in ("transcript_path", "candidates_path", "metadata_path", "music_path"):
            value = getattr(manifest, field)
            if value:
                supplied_hashes[field] = sha256_file(settings.resolve(value))
        encoder_version = run_command([settings.render.ffmpeg, "-version"]).stdout.splitlines()[0]
        from .factory import RENDER_VERSION
        from .captions import CAPTION_VERSION
        speech_digest = speech_model_digest(settings) if not manifest.transcript_path else None
        llm = None if manifest.candidates_path and manifest.metadata_path else LocalLLM(settings)
        model_digest = llm.digest if llm else "manual-inputs"
        input_hash = stable_id({"manifest": manifest.model_dump(), "source": source_sha,
                                "files": supplied_hashes, "speech": settings.speech.model_dump(),
                                "brain": settings.brain.model_dump(), "render": settings.render.model_dump(),
                                "version": __version__, "encoder": encoder_version,
                                "renderer_version": RENDER_VERSION, "caption_version": CAPTION_VERSION,
                                "speech_model_digest": speech_digest, "llm_model_digest": model_digest,
                                "prompts": [stable_id(prompt(n)) for n in ("selection", "metadata")]})
        previous = db.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        same = previous["input_hash"] == input_hash
        existing = db.conn.execute("SELECT * FROM artifacts WHERE job_id=? AND active=1", (job_id,)).fetchall()
        has_deleted = bool(db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_deleted_clips'").fetchone())
        if same and not existing and has_deleted and db.conn.execute(
                "SELECT 1 FROM artifacts a JOIN web_deleted_clips d ON d.clip_id=a.clip_id WHERE a.job_id=? LIMIT 1",
                (job_id,)).fetchone() and not retry:
            db.set_job(job_id, "NO_CANDIDATES")
            return {"job_id": job_id, "state": "NO_CANDIDATES", "artifacts": [], "deleted_clips": True}
        if same and not retry:
            if previous["state"] in ("PREPARED", "REVIEW_REQUIRED", "NO_CANDIDATES"):
                if not existing or all(_artifact_intact(Path(a["artifact_dir"]), a["render_sha256"]) for a in existing):
                    state = previous["state"]
                    if not previous["error"] and existing and all(json.loads((Path(a["artifact_dir"]) / "artifact.json").read_text(encoding="utf-8"))["state"] == "PREPARED" for a in existing):
                        state = "PREPARED"
                        db.set_job(job_id, state)
                    return {"job_id": job_id, "state": state, "cached": True,
                            "artifacts": [a["artifact_dir"] for a in existing]}
            if previous["attempts"] >= settings.max_job_attempts:
                return {"job_id": job_id, "state": previous["state"], "retry_limit": True}
            updated = datetime.fromisoformat(previous["updated_at"])
            if previous["state"] == "RETRY_WAIT" and utcnow() < updated + timedelta(minutes=2 ** previous["attempts"]):
                return {"job_id": job_id, "state": "RETRY_WAIT"}
        attempts = previous["attempts"] + 1 if same else 1
        db.conn.execute("UPDATE jobs SET input_hash=?,state='TRANSCRIBING',attempts=?,error=NULL,updated_at=? WHERE id=?",
                        (input_hash, attempts, iso_now(), job_id))
        duration = probe_source(settings, media)
        stage = "transcribing"
        report(stage, 0.0, duration)
        transcript = transcribe(settings, media, source_sha, duration,
                                settings.resolve(manifest.transcript_path) if manifest.transcript_path else None,
                                model_digest=speech_digest)
        stage = "selecting"
        report(stage)
        db.set_job(job_id, "SELECTING")
        clips = select_clips(settings, transcript, llm,
                             settings.resolve(manifest.candidates_path) if manifest.candidates_path else None)
        if not clips:
            retire_artifacts(db, job_id, set())
            db.set_job(job_id, "NO_CANDIDATES")
            return {"job_id": job_id, "state": "NO_CANDIDATES", "artifacts": []}
        from .factory import render_clip
        dirs, current_ids, review, frozen, skipped_deleted = [], set(), False, False, False
        for candidate in clips:
            checkpoint()
            versions = {"engine": __version__, "edit": RENDER_VERSION, "captions": CAPTION_VERSION, "transcript": transcript.version,
                        "encoder": encoder_version,
                        "model": settings.brain.model if llm else "manual", "model_digest": model_digest,
                        "selection_prompt": stable_id(prompt("selection")),
                        "metadata_prompt": stable_id(prompt("metadata"))}
            clip_id = stable_id({"source": source_sha, "start": candidate.start_s, "end": candidate.end_s,
                                 "transcript": stable_id(transcript.model_dump()), "versions": versions,
                                 "render": settings.render.model_dump(), "framing": manifest.framing,
                                 "music": supplied_hashes.get("music_path")})
            if has_deleted and db.conn.execute("SELECT 1 FROM web_deleted_clips WHERE clip_id=?", (clip_id,)).fetchone():
                skipped_deleted = True
                continue
            folder = settings.resolve(settings.paths.prepared) / clip_id
            new_folder = not db.conn.execute("SELECT 1 FROM artifacts WHERE clip_id=?", (clip_id,)).fetchone()
            submitted = None
            if db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publications'").fetchone():
                submitted = db.conn.execute("SELECT * FROM publications WHERE clip_id=?", (clip_id,)).fetchone()
            if not submitted and db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_publications'").fetchone():
                social = db.conn.execute("SELECT video_sha256 FROM social_publications WHERE clip_id=?", (clip_id,)).fetchone()
                if social:
                    submitted = {"render_sha256": social["video_sha256"]}
            if submitted:
                new_folder = False
                # Render identity excludes credits/schedule/approval metadata. Never
                # replace the exact artifact snapshot an existing intention refers to.
                if not _artifact_intact(folder, submitted["render_sha256"]):
                    raise ReviewRequired("submitted artifact changed; reconcile the existing publication")
                db.conn.execute("UPDATE artifacts SET active=1 WHERE clip_id=?", (clip_id,))
                dirs.append(str(folder))
                current_ids.add(clip_id)
                review = True
                frozen = True
                continue
            stage = "rendering"
            report(stage, 0.0, candidate.end_s - candidate.start_s)
            db.set_job(job_id, "RENDERING")
            rendered = render_clip(settings, media, transcript, candidate, manifest, folder)
            stage = "metadata"
            report(stage)
            metadata = append_credits(generate_metadata(settings, transcript, candidate, llm,
                                      settings.resolve(manifest.metadata_path) if manifest.metadata_path else None), manifest)
            approved = manifest.model_copy(deep=True)
            if not manifest.metadata_path:
                approved.metadata_approved = False  # Newly generated facts need explicit review.
            needs_review = (not approved.editorial_approved or not approved.metadata_approved or
                            (candidate.needs_visual_review and not approved.visual_approved))
            review |= needs_review
            artifact = {"clip_id": clip_id, "job_id": job_id, "source_sha256": source_sha,
                        "render_sha256": rendered["render_sha256"], "video_path": rendered["video_path"],
                        "caption_path": rendered["caption_path"], "render_spec": rendered["render_spec"],
                        "candidate": candidate.model_dump(), "versions": versions,
                        "metadata": metadata.model_dump(), "source_manifest": approved.model_dump(),
                        "state": "REVIEW_REQUIRED" if needs_review else "PREPARED", "created_at": iso_now()}
            # Finish the files and database association together once this
            # checkpoint passes. A later cancellation keeps this complete clip.
            checkpoint()
            atomic_json(folder / "artifact.json", artifact)
            atomic_json(folder / "transcript.json", transcript.model_dump())
            atomic_json(folder / "metadata.json", metadata.model_dump())
            db.conn.execute("INSERT OR REPLACE INTO artifacts(clip_id,job_id,artifact_dir,render_sha256,created_at) VALUES(?,?,?,?,?)",
                            (clip_id, job_id, str(folder), rendered["render_sha256"], iso_now()))
            dirs.append(str(folder))
            current_ids.add(clip_id)
            new_folder = False
        checkpoint()
        retire_artifacts(db, job_id, current_ids)
        if not dirs and skipped_deleted:
            db.set_job(job_id, "NO_CANDIDATES")
            return {"job_id": job_id, "state": "NO_CANDIDATES", "artifacts": [], "deleted_clips": True}
        state = "REVIEW_REQUIRED" if review else "PREPARED"
        db.set_job(job_id, state, "submitted_inputs_frozen" if frozen else None)
        return {"job_id": job_id, "state": state, "artifacts": dirs}
    except JobCancelled:
        # Only delete an unregistered generated output directory. Finished or
        # submitted clips have database records and survive cancellation.
        prepared = settings.resolve(settings.paths.prepared).resolve()
        if (new_folder and folder is not None and folder.parent.resolve() == prepared
                and re.fullmatch(r"[0-9a-f]{64}", folder.name) and not folder.is_symlink()
                and not (getattr(folder, "is_junction", lambda: False)())
                and not db.conn.execute("SELECT 1 FROM artifacts WHERE artifact_dir=?", (str(folder),)).fetchone()):
            shutil.rmtree(folder, ignore_errors=True)
        db.set_job(job_id, "CANCELLED")
        raise
    except ReviewRequired as error:
        db.set_job(job_id, "REVIEW_REQUIRED", str(error))
        log.warning("Job %s requires review: %s", job_id[:12], str(error))
        return {"job_id": job_id, "state": "REVIEW_REQUIRED", "reason": str(error)}
    except (ValidationError, FileNotFoundError) as error:
        db.set_job(job_id, "REVIEW_REQUIRED", type(error).__name__)
        log.warning("Job %s requires input review (%s)", job_id[:12], type(error).__name__)
        return {"job_id": job_id, "state": "REVIEW_REQUIRED", "reason": type(error).__name__}
    except Exception as error:
        # requests/FFmpeg exceptions may contain credential/session URLs. Log only type.
        previous = db.conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        state = "FAILED" if previous["attempts"] >= settings.max_job_attempts else "RETRY_WAIT"
        db.set_job(job_id, state, type(error).__name__)
        # Keep diagnostic locations, never exception messages, source URLs or locals.
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        db.event("job_failure", job_id, {"stage": stage, "error_type": type(error).__name__, "frames": frames})
        log.error("Job %s failed during %s (%s)", job_id[:12], stage, type(error).__name__)
        return {"job_id": job_id, "state": state, "stage": stage, "error_type": type(error).__name__}


def retire_artifacts(db: DB, job_id: str, keep_ids: set[str]):
    """A replacement selection cannot leave an older unsubmitted clip publishable."""
    has_publications = bool(db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publications'").fetchone())
    for record in db.conn.execute("SELECT * FROM artifacts WHERE job_id=? AND active=1", (job_id,)).fetchall():
        if record["clip_id"] in keep_ids:
            continue
        submitted = has_publications and db.conn.execute("SELECT 1 FROM publications WHERE clip_id=?",
                                                        (record["clip_id"],)).fetchone()
        if not submitted and db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_publications'").fetchone():
            submitted = db.conn.execute("SELECT 1 FROM social_publications WHERE clip_id=?", (record["clip_id"],)).fetchone()
        if not submitted:
            path = Path(record["artifact_dir"]) / "artifact.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                data["state"] = "SUPERSEDED"
                atomic_json(path, data)
        db.conn.execute("UPDATE artifacts SET active=0 WHERE clip_id=?", (record["clip_id"],))


def _artifact_intact(folder: Path, checksum: str) -> bool:
    try:
        artifact = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
        return sha256_file(Path(artifact["video_path"])) == checksum == artifact["render_sha256"]
    except (OSError, KeyError, ValueError):
        return False


def verify_artifact(settings: Settings, folder: Path) -> dict:
    checkpoint()
    artifact = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
    video = Path(artifact["video_path"])
    if not _artifact_intact(folder, artifact["render_sha256"]):
        raise ReviewRequired("render checksum mismatch or missing artifact")
    probe = json.loads(run_command([settings.render.ffprobe, "-v", "error", "-show_streams",
                                   "-show_format", "-of", "json", str(video)]).stdout)
    stream = next(x for x in probe["streams"] if x["codec_type"] == "video")
    if (stream["width"], stream["height"]) != (settings.render.width, settings.render.height):
        raise ReviewRequired("wrong output dimensions")
    run_command([settings.render.ffmpeg, "-nostdin", "-v", "error", "-i", str(video), "-f", "null", "-"])
    return {"clip_id": artifact["clip_id"], "checksum": "ok", "decode": "ok",
            "width": stream["width"], "height": stream["height"], "state": artifact["state"]}


def approve_artifact(folder: Path, visual_reviewed: bool, metadata_reviewed: bool):
    path = folder / "artifact.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("state") == "SUPERSEDED":
        raise ReviewRequired("superseded selection cannot be approved; prepare current source inputs")
    if not metadata_reviewed:
        raise ReviewRequired("approval requires --metadata-reviewed")
    if data["candidate"]["needs_visual_review"] and not visual_reviewed:
        raise ReviewRequired("this clip requires --visual-reviewed after inspecting framing")
    data["source_manifest"].update(editorial_approved=True, metadata_approved=True,
                                    visual_approved=visual_reviewed)
    data["state"] = "PREPARED"
    data["approved_at"] = iso_now()
    atomic_json(path, data)


def assert_editable(db: DB, folder: Path):
    data = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
    if db.conn.execute("SELECT 1 FROM publications WHERE clip_id=?", (data["clip_id"],)).fetchone():
        raise ReviewRequired("publication intention already exists; preserve its inputs and reconcile if needed")
    if db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_publications'").fetchone():
        if db.conn.execute("SELECT 1 FROM social_publications WHERE clip_id=?", (data["clip_id"],)).fetchone():
            raise ReviewRequired("social publication intention already exists; preserve its inputs and reconcile if needed")


def reserve_slot(db: DB, channel_id: str, clip_id: str, candidates: list[datetime]) -> str:
    """Atomic once-per-channel clip reservation; retries reuse the original UTC slot."""
    with db.transaction() as conn:
        prior = conn.execute("SELECT publish_at FROM slots WHERE channel_id=? AND clip_id=?",
                              (channel_id, clip_id)).fetchone()
        if prior:
            return prior["publish_at"]
        for slot in candidates:
            if slot.tzinfo is None or slot.utcoffset() is None or slot <= utcnow():
                raise ValueError("slot must be future and timezone-aware")
            stamp = slot.astimezone(timezone.utc).isoformat()
            used = conn.execute("SELECT 1 FROM slots WHERE channel_id=? AND publish_at=?", (channel_id, stamp)).fetchone()
            if not used:
                conn.execute("INSERT INTO slots VALUES(?,?,?)", (channel_id, stamp, clip_id))
                return stamp
        raise ReviewRequired("no publication slot available")


def daily_slots(timezone_name: str, hour: int, days: int = 14, lead_minutes: int = 60) -> list[datetime]:
    zone = ZoneInfo(timezone_name)
    today = utcnow().astimezone(zone).date()
    result = []
    for offset in range(days):
        local = datetime.combine(today + timedelta(days=offset), datetime.min.time()).replace(hour=hour)
        early, late = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
        # Skip ambiguous and nonexistent wall-clock slots; do not guess a DST fold.
        if early.utcoffset() != late.utcoffset():
            continue
        if early.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local:
            continue
        if early > utcnow() + timedelta(minutes=lead_minutes):
            result.append(early)
    return result


def cycle(settings: Settings, db: DB, discovery: bool = False, enable_upload: bool = False) -> dict:
    from .sourcing import discover, purge
    from .upload import expire_owner_observations
    purge(db, settings.discovery.retention_days)
    expire_owner_observations(db)
    result = {"jobs": []}
    if discovery:
        report = discover(settings, db)
        result["discovery"] = {k: v for k, v in report.items() if k != "candidates"}
    for path in sorted(settings.resolve(settings.paths.inbox).glob("*.json")):
        result["jobs"].append(prepare_manifest(settings, db, path))
    if enable_upload:
        from .upload import poll_publications, upload_prepared
        result["uploads"] = []
        folders = {a["artifact_dir"] for a in db.conn.execute("SELECT artifact_dir FROM artifacts WHERE active=1")}
        if db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publications'").fetchone():
            # Already submitted transfers retain their immutable input snapshot and must
            # recover even if later source selections have superseded their local job.
            folders.update(str(Path(p["video_path"]).parent) for p in db.conn.execute(
                "SELECT video_path FROM publications WHERE session_uri IS NOT NULL AND video_id IS NULL"))
        for value in sorted(folders):
            folder = Path(value)
            if (folder / "artifact.json").exists():
                result["uploads"].append(upload_prepared(settings, db, folder, enable_upload=True))
        result["publications"] = poll_publications(settings, db)
    return result


def setup_logs(settings: Settings):
    folder = settings.resolve(settings.paths.logs)
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / "worker.log", maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local clipping; uploads require config AND explicit flag")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve", help="Open the local browser studio")
    p.add_argument("--config", dest="serve_config", default=None,
                   help="Configuration file (created on first launch)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    sub.add_parser("init")
    p = sub.add_parser("prepare"); p.add_argument("manifest"); p.add_argument("--retry", action="store_true")
    sub.add_parser("discover")
    sub.add_parser("purge")
    p = sub.add_parser("forget-discovery"); p.add_argument("--video-id")
    sub.add_parser("status")
    for cmd in ("inspect", "verify"):
        p = sub.add_parser(cmd); p.add_argument("artifact_dir")
    p = sub.add_parser("approve"); p.add_argument("artifact_dir")
    p.add_argument("--visual-reviewed", action="store_true"); p.add_argument("--metadata-reviewed", action="store_true")
    sub.add_parser("authorize")
    p = sub.add_parser("upload"); p.add_argument("artifact_dir"); p.add_argument("--enable-upload", action="store_true")
    sub.add_parser("poll")
    sub.add_parser("revoke")
    sub.add_parser("delete-owned-data")
    p = sub.add_parser("reserve-slot"); p.add_argument("artifact_dir"); p.add_argument("--hour", type=int, default=12)
    for cmd in ("run", "loop"):
        p = sub.add_parser(cmd); p.add_argument("--discovery", action="store_true")
        p.add_argument("--enable-upload", action="store_true")
        if cmd == "loop": p.add_argument("--interval", type=int, default=60)
    args = parser.parse_args(argv)
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            parser.error("port must be between 1 and 65535")
        path = Path(args.serve_config or args.config).resolve()
        if not path.exists():
            atomic_json(path, Settings().model_dump())
        from .web import run_server
        run_server(path, port=args.port, open_browser=not args.no_browser)
        return
    if args.command == "init":
        path = Path(args.config).resolve()
        if path.exists(): parser.error("configuration already exists")
        atomic_json(path, Settings().model_dump())
    settings = load_settings(args.config)
    for value in (settings.paths.inbox, settings.paths.prepared, settings.paths.cache, settings.paths.logs):
        settings.resolve(value).mkdir(parents=True, exist_ok=True)
    setup_logs(settings)
    try:
        # Every command, including OAuth/reconciliation, shares one OS file lock.
        with FileLock(str(settings.resolve(settings.paths.state)) + ".lock", timeout=0):
            db = DB(settings.resolve(settings.paths.state))
            try:
                # Cleanup is required even for local-only commands after a long shutdown.
                from .sourcing import purge
                from .upload import expire_owner_observations
                purge(db, settings.discovery.retention_days)
                expire_owner_observations(db)
                command = args.command
                if command == "init": result = {"initialized": settings.paths.root}
                elif command == "prepare": result = prepare_manifest(settings, db, settings.resolve(args.manifest), args.retry)
                elif command == "discover":
                    from .sourcing import discover
                    result = discover(settings, db)
                elif command == "purge":
                    from .sourcing import purge
                    result = purge(db, settings.discovery.retention_days)
                elif command == "forget-discovery":
                    from .sourcing import forget_discovery
                    result = forget_discovery(db, args.video_id)
                elif command == "status":
                    result = [dict(x) for x in db.conn.execute("SELECT id,state,attempts,error,updated_at FROM jobs")]
                elif command == "inspect":
                    result = json.loads((settings.resolve(args.artifact_dir) / "artifact.json").read_text(encoding="utf-8"))
                elif command == "verify": result = verify_artifact(settings, settings.resolve(args.artifact_dir))
                elif command == "approve":
                    assert_editable(db, settings.resolve(args.artifact_dir))
                    approve_artifact(settings.resolve(args.artifact_dir), args.visual_reviewed, args.metadata_reviewed)
                    result = {"state": "PREPARED"}
                elif command == "authorize":
                    from .upload import authorize
                    result = authorize(settings)
                elif command == "upload":
                    from .upload import upload_prepared
                    result = upload_prepared(settings, db, settings.resolve(args.artifact_dir), args.enable_upload)
                elif command == "poll":
                    from .upload import poll_publications
                    result = poll_publications(settings, db)
                elif command == "revoke":
                    from .upload import revoke_owner_consent
                    result = revoke_owner_consent(settings, db)
                elif command == "delete-owned-data":
                    from .upload import delete_owner_upload_data
                    result = delete_owner_upload_data(settings, db)
                elif command == "reserve-slot":
                    if not 0 <= args.hour <= 23: parser.error("hour must be 0..23")
                    if not settings.upload.target_channel_id: parser.error("set target_channel_id before reserving")
                    folder = settings.resolve(args.artifact_dir)
                    assert_editable(db, folder)
                    data = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
                    stamp = reserve_slot(db, settings.upload.target_channel_id, data["clip_id"],
                                         daily_slots(settings.timezone, args.hour,
                                                     lead_minutes=settings.upload.min_lead_minutes))
                    data["source_manifest"]["schedule"] = stamp
                    atomic_json(folder / "artifact.json", data)
                    result = {"publish_at_utc": stamp}
                elif command == "run": result = cycle(settings, db, args.discovery, args.enable_upload)
                elif command == "loop":
                    if args.interval < 10: parser.error("interval must be at least 10 seconds")
                    while True:
                        result = cycle(settings, db, args.discovery, args.enable_upload)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        time.sleep(args.interval)
                print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            finally:
                db.close()
    except Timeout:
        print(json.dumps({"state": "ALREADY_RUNNING"}))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        log.error("Command failed (%s)", type(error).__name__)
        print(json.dumps({"state": "ERROR", "error_type": type(error).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
