"""Persistent local studio service. HTTP transport lives in :mod:`web`.

Browser records share the engine database and publication lock. File paths and
upload sessions stay on the server; only explicitly registered media has URLs.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests
from filelock import FileLock, Timeout
from pydantic import ValidationError

from . import __version__
from .common import (DB, Metadata, Settings, SourceManifest, atomic_json, iso_now,
                     load_settings, run_command, sha256_file, stable_id)
from .story_service import StoryServiceMixin, STORY_KINDS
from .story_engine import StoryError
from .story_audio import StoryAudioError
from .story_voice_service import StoryVoiceServiceMixin, VOICE_KINDS


class WebError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


EXPOSED = {
    "speech": {"model", "device", "compute_type", "language", "cpu_threads"},
    "brain": {"model", "min_clip_s", "max_clip_s", "max_clips"},
    "render": {"ffmpeg", "ffprobe", "font_size"},
    "upload": {"enabled", "target_channel_id", "client_secrets", "privacy_status"},
    "instagram": {"enabled", "credentials_file", "target_account_id", "api_version"},
    "tiktok": {"enabled", "credentials_file"},
    "twitch": {"credentials_file"},
}
MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts"}
ACTIVE = {"queued", "running"}
FINISHED = {"completed", "failed", "interrupted", "cancelled"}
CANCELLABLE = {"clip", "link_import"} | STORY_KINDS | VOICE_KINDS
RESUMABLE = {"ready", "uploading", "retry_wait", "quota_wait", "auth_required"}


def _strict_keys(value, keys):
    if not isinstance(value, dict) or not set(value) <= set(keys):
        raise WebError("The request contains unsupported fields.")


def _text(value, label, limit=1000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise WebError(f"Enter a valid {label} (up to {limit} characters).")
    if "\x00" in value:
        raise WebError(f"The {label} contains an unsupported character.")
    return value.strip()


class WebService(StoryVoiceServiceMixin, StoryServiceMixin):
    def __init__(self, config_path: Path | str, settings: Settings | None = None):
        self.config_path = Path(config_path).resolve()
        if not self.config_path.exists():
            atomic_json(self.config_path, (settings or Settings()).model_dump())
        self.settings = settings or load_settings(self.config_path)
        self.settings.paths.root = str(Path(self.settings.paths.root).resolve())
        self.token = secrets.token_urlsafe(32)
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.pending = queue.Queue()
        self.cancel_events = {}
        self.data_dir = self.settings.resolve(self.settings.paths.cache).parent / "studio"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "sources").mkdir(exist_ok=True)
        (self.data_dir / "manifests").mkdir(exist_ok=True)
        self.state_path = self.settings.resolve(self.settings.paths.state)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.instance_lock = FileLock(str(self.state_path) + ".studio.lock", timeout=0)
        try:
            self.instance_lock.acquire()
        except Timeout:
            raise WebError("ClipForge Studio is already open for this workspace. Use its existing browser window.", 409) from None
        try:
            with self.engine_lock():
                self._initialize_database()
            self.capabilities = self.health()
        except BaseException:
            self.instance_lock.release()
            raise
        self.thread = threading.Thread(target=self._work, name="clipforge-studio-worker", daemon=True)
        self.thread.start()

    def _initialize_database(self):
        import re
        # No server requests exist yet, and this workspace's singleton lock is held.
        # Reap only app-generated staging names left by interrupted transfers.
        for partial in (self.data_dir / "sources").glob("*.part"):
            if partial.is_file() and re.fullmatch(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,8})?\.part", partial.name):
                partial.unlink(missing_ok=True)
        with self.db() as db:
            from .upload import _schema
            _schema(db)
            from .social_publish import schema
            schema(db)
            db.conn.executescript("""
              CREATE TABLE IF NOT EXISTS web_sources(id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS web_jobs(id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS web_clips(clip_id TEXT PRIMARY KEY, source_id TEXT, job_id TEXT);
              CREATE TABLE IF NOT EXISTS web_links(id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS web_deleted_clips(clip_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS web_stories(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)
            for row in db.conn.execute("SELECT * FROM web_jobs").fetchall():
                job = json.loads(row["data"])
                if job["state"] in ACTIVE:
                    job.update(state="interrupted", stage="interrupted", message="ClipForge stopped before this task finished. Retry when ready.")
                    self._save_job(db, job)
                    if job["kind"] in STORY_KINDS:
                        self._story_job_finished(db, job)
            for row in db.conn.execute("SELECT * FROM web_links").fetchall():
                link = json.loads(row["data"])
                # Upgrade saved references without initiating network requests.
                if link["platform"] in {"youtube", "twitch", "kick"}:
                    from .link_import import describe, LinkError
                    try:
                        descriptor = describe(link["url"])
                        link.update(can_download=descriptor["can_download"], page_import=descriptor["page_import"])
                        if link["state"] == "needs_file" and descriptor["can_download"]:
                            link["message"] = "Choose Import video to download this recording onto your computer, or attach a video file."
                    except LinkError:
                        pass
                    self._save_link(db, link)
                if link["state"] == "importing":
                    link.update(state="failed", message="The download was interrupted. Retry it explicitly, or attach your authorized file.")
                    self._save_link(db, link)
            from .upload import expire_owner_observations
            from .sourcing import purge
            purge(db, self.settings.discovery.retention_days)
            expire_owner_observations(db)

    @contextlib.contextmanager
    def db(self):
        db = DB(self.state_path)
        try:
            yield db
        finally:
            db.close()

    def engine_lock(self, timeout=0):
        return FileLock(str(self.state_path) + ".lock", timeout=timeout)

    def close(self):
        self.stop.set()
        self.pending.put(None)
        self.thread.join(timeout=1)
        self.instance_lock.release()

    def public_settings(self):
        data = self.settings.model_dump()
        return {group: {key: data[group][key] for key in keys} for group, keys in EXPOSED.items()}

    def health(self):
        result = {}
        for name in ("ffmpeg", "ffprobe"):
            executable = getattr(self.settings.render, name)
            try:
                run_command([executable, "-version"], timeout=4)
                result[name] = {"ok": True, "message": f"{name} is ready."}
            except (OSError, subprocess.SubprocessError):
                result[name] = {"ok": False, "message": f"Install {name} and enter its executable path in Settings."}
        model = self.settings.resolve(self.settings.speech.model)
        speech_files = all((model / name).is_file() for name in ("model.bin", "tokenizer.json", "config.json"))
        speech_installed = importlib.util.find_spec("faster_whisper") is not None
        result["speech"] = {"ok": speech_files and speech_installed, "message":
            "Local speech model is ready." if speech_files and speech_installed else
            "AI mode needs faster-whisper and a complete local speech model folder. Manual clips work without it."}
        if speech_files and speech_installed:
            from .brain import check_speech_runtime, ReviewRequired
            try:
                check_speech_runtime()
            except ReviewRequired as error:
                result["speech"] = {"ok": False, "message": str(error)}
            except Exception:
                result["speech"] = {"ok": False, "message": "Whisper's audio reader could not start. Repair the AI installation and restart ClipForge."}
        result["ollama"] = {"ok": False, "message": "Start Ollama and install the selected local model for AI mode. Manual clips work without it."}
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(self.settings.brain.ollama_url.rstrip("/") + "/api/tags", timeout=2, allow_redirects=False)
                if response.status_code == 200:
                    models = [m.get("name", "") for m in response.json().get("models", [])]
                    selected = self.settings.brain.model
                    if selected in models or selected + ":latest" in models:
                        result["ollama"] = {"ok": True, "message": "Ollama is running with the selected model. AI jobs verify it is fully local before processing."}
                    else:
                        result["ollama"]["message"] = "Ollama is running. Install the model selected in Settings."
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            pass
        extras = importlib.util.find_spec("google_auth_oauthlib") is not None
        ready = bool(extras and self.settings.upload.target_channel_id and self.settings.resolve(self.settings.upload.client_secrets).is_file())
        result["youtube"] = {"ok": ready, "message": "YouTube setup is ready; use Connect YouTube to authorize." if ready else
            "Optional: install YouTube dependencies, set your channel ID and Desktop OAuth client file, then connect."}
        from .link_import import twitch_status
        from .social_publish import provider_status
        result["twitch"] = twitch_status(self.settings)
        for platform in ("instagram", "tiktok"):
            result[platform] = provider_status(self.settings, platform)
        from .page_import import importer_status
        result["link_import"] = importer_status(self.settings.render.ffmpeg)
        if not result["ffmpeg"]["ok"] or not result["ffprobe"]["ok"]:
            result["link_import"] = {"ok": False, "message": "Set up FFmpeg and FFprobe in Settings before importing links."}
        self.capabilities = result
        return result

    def bootstrap(self):
        return {"token": self.token, "version": __version__, "workspace": str(self.settings.resolve(".")),
                "settings": self.public_settings(), "capabilities": self.capabilities, **self.library()}

    def _sources(self, db):
        return [json.loads(r["data"]) for r in db.conn.execute("SELECT * FROM web_sources ORDER BY rowid DESC")]

    def _source(self, db, source_id):
        row = db.conn.execute("SELECT data FROM web_sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise WebError("This video is no longer in the library.", 404)
        return json.loads(row["data"])

    def _managed_source_path(self, source):
        path = Path(source["path"])
        folder = self.data_dir / "sources"
        root = folder.resolve()
        if (not folder.is_symlink() and not path.is_symlink() and path.resolve().parent == root
                and re.fullmatch(r"[0-9a-f]{32}\.[a-z0-9]{1,8}", path.name)):
            return path
        return None

    def source_record(self, source, busy=False):
        record = {k: v for k, v in source.items() if k not in {"path", "sha256"}}
        available = not source.get("source_deleted") and Path(source["path"]).is_file()
        managed = self._managed_source_path(source) is not None
        record.update(available=bool(available), managed_file=managed,
                      can_delete_file=bool(available and managed and not busy))
        if not available:
            record["url"] = None
        return record

    def _save_job(self, db, job):
        event = self.cancel_events.get(job["id"])
        if event is not None and event.is_set() and job["state"] in ACTIVE:
            job["cancel_requested"] = True
        db.conn.execute("INSERT OR REPLACE INTO web_jobs VALUES(?,?)", (job["id"], json.dumps(job, allow_nan=False)))

    def _job(self, db, job_id):
        row = db.conn.execute("SELECT data FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise WebError("This task could not be found.", 404)
        return json.loads(row["data"])

    def job_record(self, job, db):
        from .progress import public_timing
        record = {k: v for k, v in job.items() if k not in {"payload", "engine_id", "kind", "retry_requested", "_timing", "timing_profile", "stage_work_totals", "last_progress_save"}}
        record["can_delete"] = job["state"] in FINISHED
        record["can_cancel"] = job["kind"] in CANCELLABLE and job["state"] in ACTIVE and not job.get("cancel_requested")
        if job["kind"] in STORY_KINDS:
            record["project_id"] = job["payload"].get("project_id")
            record["kind"] = job["kind"]
        if job["kind"] in VOICE_KINDS:
            record["kind"] = job["kind"]
            record["voice_catalog_id"] = job["payload"].get("id")
            record["voice_id"] = job["payload"].get("voice")
        record["source_id"] = self._job_source_id(db, job)
        if job.get("kind", "").startswith("social_"):
            record["platform"] = job["payload"]["platform"]
            record["target_clip_id"] = job["payload"]["clip_id"]
        if job["state"] == "running" and job.get("engine_id") and not job.get("_timing"):
            row = db.conn.execute("SELECT state FROM jobs WHERE id=?", (job["engine_id"],)).fetchone()
            if row and row["state"] in {"TRANSCRIBING", "SELECTING", "RENDERING"}:
                stage = row["state"].lower()
                record.update(stage=stage, progress={"transcribing": 20, "selecting": 45, "rendering": 65}[stage],
                              message={"transcribing": "Transcribing speech locally…", "selecting": "Finding clips with your local model…", "rendering": "Rendering vertical clips…"}[stage])
        history = [json.loads(row["data"]) for row in db.conn.execute("SELECT data FROM web_jobs ORDER BY rowid DESC LIMIT 100")]
        record.update(public_timing(job, history))
        if job.get("cancel_requested") and job["state"] in ACTIVE:
            record["message"] = "Stopping this task and cleaning up unfinished files…"
        return record

    def _job_source_id(self, db, job):
        if job.get("source_id"):
            return job["source_id"]
        if job.get("kind") == "link_import":
            row = db.conn.execute("SELECT data FROM web_links WHERE id=?", (job.get("payload", {}).get("link_id"),)).fetchone()
            if row:
                return json.loads(row["data"]).get("source_id")
        return None

    def _artifact(self, db, clip_id):
        row = db.conn.execute("SELECT * FROM artifacts WHERE clip_id=? AND active=1", (clip_id,)).fetchone()
        if not row:
            raise WebError("This clip could not be found.", 404)
        folder = Path(row["artifact_dir"])
        try:
            return folder, json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise WebError("The prepared clip is unavailable. Retry its task.", 409) from None

    def _clip_record(self, db, artifact):
        clip_id = artifact["clip_id"]
        meta, candidate = artifact.get("metadata", {}), artifact.get("candidate", {})
        link = db.conn.execute("SELECT * FROM web_clips WHERE clip_id=?", (clip_id,)).fetchone()
        publication = db.conn.execute("SELECT * FROM publications WHERE clip_id=? ORDER BY updated_at DESC LIMIT 1", (clip_id,)).fetchone()
        from .upload import _public
        source = self._source(db, link["source_id"]) if link and link["source_id"] else {}
        render = artifact.get("render_spec", {}).get("render", {})
        can_resume, resume_message = self._resume_availability(artifact, dict(publication) if publication else None)
        from .social_publish import public_records
        social = public_records(db, clip_id)
        can_delete, delete_message = self._clip_delete_status(db, clip_id)
        return {"id": clip_id, "job_id": link["job_id"] if link else artifact.get("job_id"), "title": meta.get("title", "Untitled clip"),
                "description": meta.get("description", ""), "tags": meta.get("tags", []),
                "duration_s": float(candidate.get("end_s", 0)) - float(candidate.get("start_s", 0)),
                "width": render.get("width", self.settings.render.width), "height": render.get("height", self.settings.render.height),
                "state": artifact.get("state", "REVIEW_REQUIRED"), "mode": artifact.get("web_mode", "ai"),
                "created_at": artifact.get("created_at"), "video_url": f"/media/clip/{clip_id}",
                "download_url": f"/media/clip/{clip_id}?download=1", "caption_url": f"/media/caption/{clip_id}" if artifact.get("caption_path") else None,
                "framing": artifact.get("source_manifest", {}).get("framing", "center_crop"),
                "start_s": candidate.get("start_s", 0), "end_s": candidate.get("end_s", 0), "reason": candidate.get("reason", ""),
                "submitted": bool(publication or social), "social_publications": social,
                "is_demo": bool(source.get("is_demo") or artifact.get("web_is_demo")),
                "can_resume": can_resume, "resume_message": resume_message,
                "can_delete": can_delete, "delete_message": delete_message,
                "publication": _public(dict(publication)) if publication else None}

    def _clip_delete_status(self, db, clip_id):
        if self._active(db):
            return False, "Wait for the current tasks to finish before deleting clip files."
        for row in db.conn.execute("SELECT state, video_id FROM publications WHERE clip_id=?", (clip_id,)):
            if not row["video_id"]:
                return False, "This clip has an unfinished or uncertain YouTube upload. Resolve that upload before deleting its files."
        for row in db.conn.execute("SELECT state FROM social_publications WHERE clip_id=?", (clip_id,)):
            if row["state"] not in {"published", "failed", "rejected"}:
                return False, "Finish or resolve this clip's platform upload before deleting its files."
        return True, "Delete this finished clip and its captions from this computer."

    @staticmethod
    def _intent_fingerprint(publication):
        return stable_id({key: publication.get(key) for key in (
            "clip_id", "channel_id", "source_sha256", "render_sha256", "artifact_json", "metadata_json",
            "video_path", "total_bytes", "publish_at")})

    def _resume_availability(self, artifact, publication):
        if not publication:
            return False, "No saved publication exists for this clip."
        if (not self.settings.upload.enabled
                or self.settings.upload.target_channel_id != publication["channel_id"]):
            return False, "Enable publishing with this publication's original target channel to resume."
        if publication.get("video_id"):
            return False, "YouTube has accepted this upload. Refresh publishing status."
        if publication["state"] not in RESUMABLE:
            return False, "This publication requires reconciliation or review before any further upload."
        try:
            if stable_id(artifact) != stable_id(json.loads(publication["artifact_json"])):
                return False, "The saved artifact has changed. Reconcile the existing publication."
        except (ValueError, TypeError):
            return False, "The saved publication snapshot is unavailable. Reconciliation is required."
        from .upload import _held
        if _held(self.settings, publication):
            return False, ("Reconnect the original target channel before resuming this upload." if publication["state"] == "auth_required"
                           else "Wait until the saved retry time before resuming this upload.")
        return True, "Resume the existing upload with its saved channel, metadata, audience and schedule."

    def library(self):
        with self.db() as db:
            busy = self._active(db)
            sources = [self.source_record(s, busy) for s in self._sources(db)]
            jobs = [self.job_record(json.loads(r["data"]), db) for r in db.conn.execute("SELECT data FROM web_jobs ORDER BY rowid DESC")]
            clips = []
            for row in db.conn.execute("SELECT clip_id FROM artifacts WHERE active=1 ORDER BY created_at DESC"):
                try:
                    _, artifact = self._artifact(db, row["clip_id"])
                    clips.append(self._clip_record(db, artifact))
                except WebError:
                    pass
            links = [self.link_record(json.loads(row["data"])) for row in db.conn.execute("SELECT data FROM web_links ORDER BY rowid DESC")]
            return {"sources": sources, "jobs": jobs, "clips": clips, "links": links}

    @staticmethod
    def link_record(link):
        return {key: value for key, value in link.items() if key not in {"private_url", "clip_id", "page_import"}}

    def _save_link(self, db, link):
        db.conn.execute("INSERT OR REPLACE INTO web_links VALUES(?,?)", (link["id"], json.dumps(link)))

    def _link(self, db, link_id):
        row = db.conn.execute("SELECT data FROM web_links WHERE id=?", (link_id,)).fetchone()
        if not row:
            raise WebError("This saved link could not be found.", 404)
        return json.loads(row["data"])

    def import_link(self, payload):
        from .link_import import LinkError, describe, twitch_status
        _strict_keys(payload, {"url", "rights_confirmed"})
        if payload.get("rights_confirmed") is not True:
            raise WebError("Confirm you have permission to download and edit this video.")
        try:
            descriptor = describe(payload.get("url"))
        except LinkError as exc:
            raise WebError(str(exc)) from None
        with self.guard, self.db() as db:
            link = dict(descriptor, id=uuid.uuid4().hex, created_at=iso_now(), state="needs_file", source_id=None)
            if descriptor["platform"] == "direct":
                # Signed query strings stay in local private records, never the browser library.
                link["private_url"] = payload["url"]
            importer = self.capabilities.get("link_import", {})
            page_ready = importer.get("ok") and (descriptor["platform"] != "youtube" or importer.get("youtube_ready", importer.get("ok")))
            ready = (descriptor["platform"] == "direct"
                     or (descriptor.get("page_import") and page_ready)
                     or (descriptor["clip_id"] and twitch_status(self.settings)["ok"]))
            if ready:
                link.update(state="importing", message="Downloading the authorized video into your local library…")
            elif descriptor.get("page_import"):
                link["message"] = "Check Link importer in Settings, then choose Import video. You can also attach a local video file."
            self._save_link(db, link)
            result = {"link": self.link_record(link)}
            if ready:
                try:
                    result.update(self._enqueue(db, "link_import", {"link_id": link["id"]}, title="Import video link"))
                except WebError:
                    link.update(state="failed", message="The import queue is full. Retry this saved link when tasks finish.")
                    self._save_link(db, link)
                    raise
            return result

    def attach_link(self, link_id, payload):
        _strict_keys(payload, {"source_id"})
        with self.guard, self.db() as db:
            link = self._link(db, link_id)
            if link["state"] == "importing":
                raise WebError("Wait for this link download to finish before attaching a different file.", 409)
            source = self._source(db, _text(payload.get("source_id"), "video source", 128))
            if source.get("source_deleted") or not Path(source["path"]).is_file():
                raise WebError("The attached video was moved or deleted. Add it again.", 409)
            source["source_url"] = link["url"]
            db.conn.execute("UPDATE web_sources SET data=? WHERE id=?", (json.dumps(source), source["id"]))
            link.update(state="ready", source_id=source["id"], message="Your video file is attached and ready to edit.")
            self._save_link(db, link)
            return {"link": self.link_record(link), "source": self.source_record(source)}

    def retry_link(self, link_id, payload):
        from .link_import import twitch_status, describe
        _strict_keys(payload, set())
        with self.guard, self.db() as db:
            link = self._link(db, link_id)
            try:
                descriptor = describe(link["url"])
            except ValueError:
                raise WebError("This saved link is no longer supported. Attach a local video instead.", 409) from None
            link.update(can_download=descriptor["can_download"], page_import=descriptor["page_import"], clip_id=descriptor["clip_id"])
            if link["state"] == "ready" and link.get("source_id"):
                source = self._source(db, link["source_id"])
                if not self.source_record(source)["available"]:
                    link.update(state="needs_file", source_id=None)
            if link["state"] not in {"failed", "needs_file"} or not link["can_download"]:
                raise WebError("This link cannot be downloaded. Attach an authorized video file instead.", 409)
            official = bool(link.get("clip_id") and twitch_status(self.settings)["ok"])
            if descriptor.get("page_import") and not official:
                importer = self.health()["link_import"]
                if not importer["ok"] or (link["platform"] == "youtube" and not importer.get("youtube_ready", importer["ok"])):
                    raise WebError("Check Link importer in Settings before importing this video.", 409)
            link.update(state="importing", message="Downloading the authorized video into your local library…")
            self._save_link(db, link)
            try:
                result = self._enqueue(db, "link_import", {"link_id": link_id}, title="Import video link")
            except WebError:
                link.update(state="failed", message="The import queue is full. Retry this saved link when tasks finish.")
                self._save_link(db, link)
                raise
            return {"link": self.link_record(link), **result}

    def delete_link(self, link_id, payload):
        _strict_keys(payload, {"confirmed", "delete_source"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm you want to delete this saved link.")
        if type(payload.get("delete_source", False)) is not bool:
            raise WebError("Choose whether to delete the source video file.")
        with self.guard, self.db() as db:
            link = self._link(db, link_id)
            importing = link["state"] == "importing"
            for row in db.conn.execute("SELECT data FROM web_jobs"):
                job = json.loads(row["data"])
                if (job.get("kind") == "link_import" and job["state"] in ACTIVE
                        and job.get("payload", {}).get("link_id") == link_id):
                    importing = True
                    break
            if importing:
                raise WebError("Wait for this link import to finish before deleting the saved link.", 409)
            if payload.get("delete_source"):
                if not link.get("source_id"):
                    raise WebError("This saved link has no source video to delete.", 409)
                self.delete_source(link["source_id"], {"confirmed": True})
            # The saved reference is independent of sources, files and artifacts.
            # Source provenance and publication history must remain intact.
            db.conn.execute("DELETE FROM web_links WHERE id=?", (link_id,))
            return {"deleted": link_id}

    def delete_job(self, job_id, payload):
        _strict_keys(payload, {"confirmed", "delete_source"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm you want to delete this workshop item.")
        if type(payload.get("delete_source", False)) is not bool:
            raise WebError("Choose whether to delete the source video file.")
        with self.guard, self.db() as db:
            job = self._job(db, job_id)
            if job["state"] not in FINISHED:
                raise WebError("Wait for this task to finish before deleting it.", 409)
            if payload.get("delete_source"):
                source_id = self._job_source_id(db, job)
                if not source_id:
                    raise WebError("This workshop item has no source video to delete.", 409)
                self.delete_source(source_id, {"confirmed": True})
            # Only remove workshop history. Artifacts and publication records
            # have independent lifetimes and must survive this deletion.
            self._delete_voice_preview(job)
            db.conn.execute("DELETE FROM web_jobs WHERE id=?", (job_id,))
            return {"deleted": job_id}

    def cancel_job(self, job_id, payload):
        _strict_keys(payload, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm you want to stop this task.")
        with self.guard, self.db() as db:
            job = self._job(db, job_id)
            if job["kind"] not in CANCELLABLE:
                raise WebError("This action cannot be stopped here. Follow its saved publication status.", 409)
            if job["state"] not in ACTIVE:
                raise WebError("This task has already stopped.", 409)
            event = self.cancel_events.setdefault(job_id, threading.Event())
            event.set()
            job["cancel_requested"] = True
            if job["state"] == "queued":
                self._mark_cancelled(db, job)
            else:
                job["message"] = "Stopping this task and cleaning up unfinished files…"
            self._save_job(db, job)
            return {"job": self.job_record(job, db)}

    def _mark_cancelled(self, db, job):
        job.update(state="cancelled", stage="cancelled", progress=0, cancel_requested=False, message="Task stopped. Finished clips and source videos are kept.")
        if job["kind"] == "link_import":
            link = self._link(db, job["payload"]["link_id"])
            link.update(state="failed", message="Download cancelled. Choose Retry import to start again.")
            self._save_link(db, link)
        if job["kind"] in STORY_KINDS:
            job["message"] = "Story task stopped. Your saved storyboard and completed exports are kept."
            self._story_job_finished(db, job)
        if job["kind"] in VOICE_KINDS:
            job["message"] = "Voice task stopped. Installed voices and saved stories are kept."

    def _keep_completed_clips(self, db, job):
        """A cancellation stops future work without discarding completed renders."""
        from .worker import _artifact_intact
        for row in db.conn.execute("SELECT * FROM artifacts WHERE job_id=? AND active=1", (job.get("engine_id", job["id"]),)).fetchall():
            folder = Path(row["artifact_dir"])
            if _artifact_intact(folder, row["render_sha256"]):
                db.conn.execute("INSERT OR REPLACE INTO web_clips VALUES(?,?,?)", (row["clip_id"], job["source_id"], job["id"]))
                if row["clip_id"] not in job["clip_ids"]:
                    job["clip_ids"].append(row["clip_id"])

    def _clean_unfinished_manual(self, db, job):
        if job["kind"] != "clip" or job.get("mode") != "manual":
            return
        clip_id = "manual-" + job["id"]
        if db.conn.execute("SELECT 1 FROM artifacts WHERE clip_id=?", (clip_id,)).fetchone():
            return
        root = self.settings.resolve(self.settings.paths.prepared)
        folder = root / clip_id
        if not folder.exists():
            return
        try:
            def redirected(path):
                return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
            if redirected(root) or redirected(folder) or folder.resolve().parent != root.resolve():
                return
            remaining = [folder]
            while remaining:
                for path in remaining.pop().iterdir():
                    if redirected(path):
                        return
                    if path.is_dir():
                        remaining.append(path)
            if any(Path(source["path"]).resolve().is_relative_to(folder.resolve()) for source in self._sources(db)):
                return
            shutil.rmtree(folder)
        except OSError:
            job["cleanup_pending"] = True

    def delete_clip(self, clip_id, payload):
        _strict_keys(payload, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm you want to delete this finished clip and its captions from this computer.")
        try:
            with self.guard, self.engine_lock(), self.db() as db:
                folder, artifact = self._artifact(db, clip_id)
                allowed, message = self._clip_delete_status(db, clip_id)
                if not allowed:
                    raise WebError(message, 409)
                root = self.settings.resolve(self.settings.paths.prepared)
                # Only a direct, real artifact directory inside configured output
                # storage may be removed. Never follow reparse points or a source.
                def redirected(path):
                    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
                if (redirected(root) or redirected(folder) or not folder.is_dir()
                        or folder.resolve().parent != root.resolve()
                        or folder.name != clip_id):
                    raise WebError("This clip is outside ClipForge's managed output folder and cannot be deleted here.", 409)
                entries, remaining = [], [folder]
                while remaining:
                    for path in remaining.pop().iterdir():
                        if redirected(path):
                            raise WebError("This clip folder contains a linked file or directory and cannot be deleted safely.", 409)
                        entries.append(path)
                        if path.is_dir():
                            remaining.append(path)
                for source in self._sources(db):
                    if Path(source["path"]).resolve().is_relative_to(folder.resolve()):
                        raise WebError("This folder also contains a registered source video. Move the source before deleting this clip.", 409)
                other = db.conn.execute("SELECT artifact_dir FROM artifacts WHERE clip_id<>?", (clip_id,)).fetchall()
                if any(Path(row["artifact_dir"]).resolve().is_relative_to(folder.resolve()) for row in other):
                    raise WebError("This output folder is shared by another clip and cannot be deleted here.", 409)
                freed = sum(path.stat().st_size for path in entries if path.is_file())
                # Keep the artifact identity and publication history as tombstones
                # so retrying an old job cannot recreate or re-upload this clip.
                try:
                    shutil.rmtree(folder)
                except OSError:
                    # Windows may remove some metadata before an open video
                    # blocks deletion. Keep the local entry usable for retry.
                    try:
                        atomic_json(folder / "artifact.json", artifact)
                    except OSError:
                        pass
                    raise WebError("The clip files could not all be deleted. Close other apps using the clip, then retry.", 409) from None
                with db.transaction():
                    db.conn.execute("INSERT OR REPLACE INTO web_deleted_clips VALUES(?,?)", (clip_id, iso_now()))
                    db.conn.execute("UPDATE artifacts SET active=0 WHERE clip_id=?", (clip_id,))
                    db.conn.execute("DELETE FROM web_clips WHERE clip_id=?", (clip_id,))
                    for row in db.conn.execute("SELECT data FROM web_jobs").fetchall():
                        job = json.loads(row["data"])
                        if clip_id in job.get("clip_ids", []):
                            job["clip_ids"].remove(clip_id)
                            self._save_job(db, job)
                return {"deleted": clip_id, "bytes_freed": freed}
        except Timeout:
            raise WebError("A task is using this workspace. Wait for it to finish before deleting clip files.", 409) from None

    def _import_link_task(self, db, job):
        from .link_import import LinkError, VIDEO_EXTENSIONS, download_video, twitch_download, twitch_status, describe
        from .job_control import checkpoint, JobCancelled
        import urllib.parse
        link = self._link(db, job["payload"]["link_id"])
        target = None
        try:
            if link.get("source_id"):
                source = self._source(db, link["source_id"])
                if self.source_record(source)["available"]:
                    job["source_id"] = source["id"]
                    link.update(state="ready", message="This source is already available in your library.")
                    self._save_link(db, link)
                    job["result_message"] = link["message"]
                    return
                link["source_id"] = None
            url = link.get("private_url", link["url"])
            name = "Imported video"
            official = bool(link.get("clip_id") and twitch_status(self.settings)["ok"])
            if official:
                url, name = twitch_download(self.settings, link["clip_id"])
            extension = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.lower()
            if extension not in VIDEO_EXTENSIONS:
                extension = ".mp4"
            target = self.data_dir / "sources" / (uuid.uuid4().hex + extension)
            last_progress = [0.0, ""]

            def progress(stage, received=0, total=None):
                checkpoint()
                now = time.monotonic()
                if stage == last_progress[1] and now - last_progress[0] < .5:
                    return
                last_progress[:] = [now, stage]
                if job.get("stage") != stage:
                    self._stage(db, job, stage)
                job.update(download_bytes=int(received), download_total_bytes=total)
                from .progress import update_progress
                if stage == "downloading":
                    update_progress(job, received, total)
                if stage == "downloading":
                    job["message"] = f"Downloading to this computer · {received / 1024**2:.1f} MiB received…"
                    job["progress"] = min(85, 10 + 75 * received / total) if total else 20
                self._save_job(db, job)

            if describe(link["url"]).get("page_import") and not official:
                from .page_import import page_download
                result = page_download(url, target, ffmpeg=self.settings.render.ffmpeg,
                                       cancelled=lambda: self.stop.is_set() or self.cancel_events[job["id"]].is_set(), progress=progress)
                if Path(result["path"]).resolve() != target.resolve():
                    raise LinkError("The downloaded video could not be registered safely.")
                name = result["title"]
            else:
                self._stage(db, job, "downloading")
                download_video(url, target, twitch=official,
                               cancelled=lambda: self.stop.is_set() or self.cancel_events[job["id"]].is_set(),
                               progress=lambda amount: progress("downloading", amount, job.get("download_total_bytes")),
                               progress_details=lambda amount, total: progress("downloading", amount, total))
            checkpoint()
            self._stage(db, job, "verifying")
            source = self.register_source(target, name + extension)
            job["source_id"] = source["id"]
            # Use this worker connection to install the complete association together.
            raw = self._source(db, source["id"])
            raw["source_url"] = link["url"]
            db.conn.execute("UPDATE web_sources SET data=? WHERE id=?", (json.dumps(raw), source["id"]))
            link.update(state="ready", source_id=source["id"], message="Video imported into your local library. Choose Create clip to edit it.")
            self._save_link(db, link)
            job["result_message"] = link["message"]
        except Exception as exc:
            if target:
                target.unlink(missing_ok=True)
            if isinstance(exc, JobCancelled):
                raise
            checkpoint()
            message = str(exc) if isinstance(exc, (LinkError, WebError)) else "The video import could not finish. Check the link and local tools, then retry explicitly."
            link.update(state="failed", message=message)
            self._save_link(db, link)
            raise WebError(message) from None

    def delete_source(self, source_id, payload):
        _strict_keys(payload, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm you want to delete this source video. Finished clips will be kept.")
        try:
            with self.guard, self.engine_lock(), self.db() as db:
                if self._active(db):
                    raise WebError("Wait for the current tasks to finish before deleting a source video.", 409)
                source = self._source(db, source_id)
                path = self._managed_source_path(source)
                if path is None:
                    raise WebError("Only downloaded or uploaded source files managed by ClipForge can be deleted here. Original files imported by path are kept.", 409)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    raise WebError("The source file could not be deleted. Close other apps using it and try again.", 409) from None
                # Keep source provenance and artifact associations as tombstones.
                # Rendered MP4s/captions live in separate artifact directories.
                for item in self._sources(db):
                    if Path(item["path"]).resolve() == path.resolve():
                        item.update(source_deleted=True, deleted_at=iso_now())
                        db.conn.execute("UPDATE web_sources SET data=? WHERE id=?", (json.dumps(item), item["id"]))
                        if item["id"] == source_id:
                            source = item
                        for row in db.conn.execute("SELECT data FROM web_links").fetchall():
                            link = json.loads(row["data"])
                            if link.get("source_id") == item["id"]:
                                link.update(state="needs_file", source_id=None, message="Source video deleted. Finished clips are kept. Import or attach a video to make more clips.")
                                self._save_link(db, link)
                return {"deleted": source_id, "source": self.source_record(source)}
        except Timeout:
            raise WebError("A ClipForge task is using this workspace. Try deleting the source after it finishes.", 409) from None

    def get_clip(self, clip_id):
        with self.db() as db:
            return self._clip_record(db, self._artifact(db, clip_id)[1])

    def register_source(self, path, name=None, is_demo=False):
        path = Path(path).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            raise WebError("Choose an existing MP4, MOV, MKV, WebM or another supported video file.")
        try:
            info = json.loads(run_command([self.settings.render.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                "-show_format", "-show_streams", "-of", "json", str(path)], timeout=45).stdout)
            video = next(s for s in info["streams"] if s.get("codec_type") == "video")
            duration = float(info["format"]["duration"])
            if not math.isfinite(duration) or duration < 1 or not video.get("width") or not video.get("height"):
                raise ValueError
        except FileNotFoundError:
            raise WebError("FFprobe is missing. Set its executable path in Settings before adding videos.", 409) from None
        except (subprocess.SubprocessError, KeyError, ValueError, TypeError, StopIteration):
            raise WebError("This file could not be read as a video of at least one second. Check the file and FFprobe setup.") from None
        source_id = uuid.uuid4().hex
        # Preserve the sample restriction even if its file is imported or uploaded
        # directly instead of using the dedicated sample button.
        for sample in (Path(__file__).resolve().parent.parent / "demo" / "preview.mp4",
                       Path(__file__).with_name("web_static") / "sample.mp4"):
            if sample.is_file() and (path == sample.resolve() or
                    (path.stat().st_size == sample.stat().st_size and sha256_file(path) == sha256_file(sample))):
                is_demo = True
                break
        source = {"id": source_id, "name": _text(name or path.name, "video name", 240), "path": str(path),
                  "duration_s": duration, "width": int(video["width"]), "height": int(video["height"]),
                  "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]), "bytes": path.stat().st_size,
                  "url": f"/media/source/{source_id}", "created_at": iso_now(), "is_demo": bool(is_demo)}
        with self.db() as db:
            db.conn.execute("INSERT INTO web_sources VALUES(?,?)", (source_id, json.dumps(source)))
        return self.source_record(source)

    def demo(self):
        path = Path(__file__).resolve().parent.parent / "demo" / "preview.mp4"
        if not path.is_file():
            path = Path(__file__).with_name("web_static") / "sample.mp4"
        if not path.is_file():
            raise WebError("The sample video is missing. Use the complete ClipForge download or add your own video.", 404)
        with self.db() as db:
            for source in self._sources(db):
                if source.get("is_demo") and Path(source["path"]).is_file():
                    return self.source_record(source)
        return self.register_source(path, "Synthetic sample · test pattern and tone", True)

    def _active(self, db):
        return any(json.loads(r["data"]).get("state") in ACTIVE for r in db.conn.execute("SELECT data FROM web_jobs"))

    def update_settings(self, payload):
        _strict_keys(payload, EXPOSED)
        with self.guard, self.db() as db:
            if self._active(db):
                raise WebError("Wait for the current tasks to finish before changing settings.", 409)
            data = self.settings.model_dump()
            for group, values in payload.items():
                _strict_keys(values, EXPOSED[group])
                for key, value in values.items():
                    if isinstance(value, str) and (len(value) > 2000 or "\x00" in value):
                        raise WebError("A settings value is too long or contains an unsupported character.")
                    data[group][key] = value
            try:
                settings = Settings.model_validate(data)
            except ValidationError as exc:
                error = exc.errors(include_url=False, include_input=False)[0]
                raise WebError("Check " + ".".join(map(str, error["loc"])) + ": " + error["msg"]) from None
            try:
                with self.engine_lock():
                    # Save paths.root exactly as configured, rather than rewriting
                    # all unrelated options using its resolved absolute value.
                    original = json.loads(self.config_path.read_text(encoding="utf-8"))
                    serialized = settings.model_dump()
                    serialized["paths"]["root"] = original.get("paths", {}).get("root", ".")
                    atomic_json(self.config_path, serialized)
                    self.settings = settings
            except Timeout:
                raise WebError("Another ClipForge command is using this workspace. Try again when it finishes.", 409) from None
        return {"settings": self.public_settings()}

    def enqueue_clip(self, payload):
        _strict_keys(payload, {"source_id", "mode", "title", "framing", "start_s", "end_s", "caption_text", "rights_category", "permission_reference", "creator_credit", "contribution"})
        with self.guard, self.db() as db:
            source = self._source(db, _text(payload.get("source_id"), "source", 128))
            if source.get("source_deleted") or not Path(source["path"]).is_file():
                raise WebError("The original video was moved or deleted. Add it to the library again.", 409)
            mode = payload.get("mode", "manual")
            if mode not in {"manual", "ai"}:
                raise WebError("Choose manual or AI mode.")
            title = _text(payload.get("title"), "clip title", 100)
            if any(c in title for c in "<>\r\n"):
                raise WebError("The title cannot include angle brackets or line breaks.")
            source_input = {key: payload.get(key) for key in ("rights_category", "permission_reference", "creator_credit", "contribution", "framing")}
            try:
                SourceManifest(media_path=source["path"], **source_input)
            except ValidationError:
                raise WebError("Confirm the source rights, permission reference, creator credit, contribution and framing.") from None
            for key in ("permission_reference", "creator_credit", "contribution"):
                _text(source_input[key], key.replace("_", " "), 1000)
            if mode == "manual":
                start, end = payload.get("start_s"), payload.get("end_s")
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)):
                    raise WebError("Enter valid start and end times in seconds.")
                if start < 0 or end > source["duration_s"] + .001 or not 1 <= end - start <= 180:
                    raise WebError("Choose a cut between 1 and 180 seconds within this video.")
                _text(payload.get("caption_text", ""), "manual caption", 500, False)
            elif not source["has_audio"]:
                raise WebError("AI clipping needs a source with speech audio. Use manual mode for this video.")
            return self._enqueue(db, "clip", dict(payload, mode=mode, title=title), source, title)

    def _enqueue(self, db, kind, payload, source=None, title=None):
        if sum(json.loads(r["data"])["state"] in ACTIVE for r in db.conn.execute("SELECT data FROM web_jobs")) >= 20:
            raise WebError("The queue is full. Let a few tasks finish before adding more.", 409)
        job = {"id": uuid.uuid4().hex, "kind": kind, "payload": payload,
               "source_id": source["id"] if source else None,
               "source_name": source["name"] if source else ("AI Story Generator" if kind in STORY_KINDS else "Local voices" if kind in VOICE_KINDS else "Video link" if kind == "link_import" else payload.get("platform", "YouTube").title()),
               "title": title or kind.replace("_", " ").title(), "mode": payload.get("mode", kind),
               "state": "queued", "stage": "queued", "message": "Waiting for the local worker…", "progress": 0,
               "created_at": iso_now(), "clip_ids": []}
        if kind in CANCELLABLE:
            self.cancel_events[job["id"]] = threading.Event()
        if kind == "clip":
            job["timing_profile"] = stable_id({"speech": self.settings.speech.model_dump(), "brain": self.settings.brain.model_dump(), "render": self.settings.render.model_dump()})
            job["stage_work_totals"] = {"transcribing": source["duration_s"]}
            if payload.get("mode") == "manual":
                job["stage_work_totals"]["rendering"] = payload["end_s"] - payload["start_s"]
        if kind == "clip" and payload.get("mode") == "ai":
            job["engine_id"] = stable_id(str((self.data_dir / "manifests" / (job["id"] + ".json")).resolve()))
        self._save_job(db, job)
        self.pending.put(job["id"])
        return {"job": self.job_record(job, db)}

    def retry(self, job_id):
        with self.guard, self.db() as db:
            job = self._job(db, job_id)
            if job["state"] not in {"failed", "interrupted", "cancelled"}:
                raise WebError("Only failed, interrupted or cancelled tasks can be retried.", 409)
            if job["kind"] in STORY_KINDS:
                if job["kind"] == "story_ideas":
                    return self.story_ideas(job["payload"])
                action = {"story": "generate", "story_rewrite": "rewrite", "story_scene": "scene"}[job["kind"]]
                return self.story_action(job["payload"]["project_id"], action, {}, job["payload"].get("scene_id"))
            if job["kind"] in VOICE_KINDS:
                return self.story_voice_action("install" if job["kind"] == "story_voice_install" else "preview", job["payload"])
            if job["kind"] != "clip":
                raise WebError("Use the saved link or platform publishing controls to continue this task explicitly.", 409)
            from .progress import start_stage
            start_stage(job, "queued")
            self.cancel_events[job_id] = threading.Event()
            job.update(state="queued", stage="queued", message="Waiting to retry…", progress=0, cancel_requested=False)
            for key in ("result_message", "download_bytes", "download_total_bytes", "last_progress_save"):
                job.pop(key, None)
            job["retry_requested"] = True
            self._save_job(db, job)
            self.pending.put(job_id)
            return {"job": self.job_record(job, db)}

    def _stage(self, db, job, stage):
        from .progress import start_stage
        start_stage(job, stage)
        messages = {"preparing": "Checking the source and local tools…", "rendering": "Rendering your vertical clip…",
                    "transcribing": "Transcribing speech locally…", "selecting": "Finding clips with your local model…",
                    "metadata": "Writing titles and descriptions with your local model…",
                    "verifying": "Checking the rendered clip…", "authorizing": "Complete YouTube consent in the browser window…",
                    "publishing": "Sending the reviewed clip to YouTube…", "polling": "Checking YouTube processing status…", "revoking": "Disconnecting YouTube…",
                    "downloading": "Downloading the authorized video into your local library…",
                    "resolving": "Reading the video page for accessible media…", "merging": "Combining the downloaded audio and video locally…",
                    "social_publishing": "Sending the reviewed clip to the selected platform…",
                    "social_polling": "Checking the selected platform's processing status…",
                    "story_writing": "Writing your story and storyboard with the local model…",
                    "story_narrating": "Recording your story with the selected local narrator…",
                    "story_audio": "Mixing narration, original background music and sound effects…",
                    "story_rendering": "Animating your characters, camera and captions…",
                    "story_voice_download": "Downloading your selected voice for offline narration…",
                    "story_voice_preview": "Recording a short sample with your selected voice…"}
        job.update(stage=stage, message=messages.get(stage, stage.title()), progress={"preparing": 5, "rendering": 40, "verifying": 90}.get(stage, 20))
        self._save_job(db, job)

    def _job_progress(self, db, job, stage, completed=None, total=None):
        from .progress import update_progress
        changed = job.get("stage") != stage
        if changed:
            self._stage(db, job, stage)
        if completed is not None:
            update_progress(job, completed, total)
        now = time.monotonic()
        if changed or now - job.get("last_progress_save", 0) >= .5:
            job["last_progress_save"] = now
            self._save_job(db, job)

    def _work(self):
        next_maintenance = time.monotonic() + 3600
        while not self.stop.is_set():
            try:
                job_id = self.pending.get(timeout=60)
            except queue.Empty:
                if time.monotonic() >= next_maintenance:
                    try:
                        with self.engine_lock(), self.db() as db:
                            from .upload import expire_owner_observations
                            from .sourcing import purge
                            purge(db, self.settings.discovery.retention_days)
                            expire_owner_observations(db)
                    except (Timeout, OSError):
                        pass
                    next_maintenance = time.monotonic() + 3600
                continue
            if job_id is None:
                break
            with self.db() as db:
                with self.guard:
                    try:
                        job = self._job(db, job_id)
                    except WebError:
                        # Cancelled queue entries may be removed from history.
                        self.cancel_events.pop(job_id, None)
                        continue
                    if job["state"] != "queued":
                        self.cancel_events.pop(job_id, None)
                        continue
                    event = self.cancel_events.setdefault(job_id, threading.Event())
                    # Cancellation/deletion cannot race a stale queued copy.
                    job.update(state="running")
                    self._save_job(db, job)
                from .job_control import JobCancelled, job_control, checkpoint
                from .progress import finish_timing
                try:
                    with self.engine_lock(), job_control(
                            cancelled=lambda: job["kind"] in CANCELLABLE and (self.stop.is_set() or event.is_set()),
                            on_progress=lambda stage, completed, total: self._job_progress(db, job, stage, completed, total)):
                        checkpoint()
                        self._stage(db, job, "preparing")
                        from .upload import expire_owner_observations
                        from .sourcing import purge
                        purge(db, self.settings.discovery.retention_days)
                        expire_owner_observations(db)
                        if job["kind"] == "clip":
                            self._prepare(db, job)
                        elif job["kind"] == "link_import":
                            self._import_link_task(db, job)
                        elif job["kind"] in STORY_KINDS:
                            self._story_task(db, job)
                        elif job["kind"] in VOICE_KINDS:
                            self._story_voice_task(db, job)
                        elif job["kind"].startswith("social_"):
                            self._social_task(db, job)
                        else:
                            self._youtube_task(db, job)
                        job.update(state="completed", stage="ready", progress=100,
                                   message=job.get("result_message", "Ready for your review."))
                except JobCancelled:
                    if job["kind"] == "clip":
                        self._keep_completed_clips(db, job)
                        self._clean_unfinished_manual(db, job)
                    self._mark_cancelled(db, job)
                    if job.get("cleanup_pending"):
                        job["message"] = "Task stopped. An unfinished clip file is still in use; close its preview and retry the task to replace it."
                except Timeout:
                    job.update(state="failed", stage="failed", message="Another ClipForge command is running. Retry when it finishes.")
                except WebError as exc:
                    job.update(state="failed", stage="failed", message=str(exc))
                except (StoryError, StoryAudioError) as exc:
                    job.update(state="failed", stage="failed", message=str(exc))
                except FileNotFoundError:
                    job.update(state="failed", stage="failed", message="A source, model or executable is missing. Check Settings and the original video, then retry.")
                except Exception as exc:
                    # Never expose subprocess stderr, credential URLs or raw HTTP exceptions.
                    job.update(state="failed", stage="failed", message=f"The task could not finish ({type(exc).__name__}). Check local prerequisites and retry.")
                if job["kind"] == "link_import" and job["state"] == "failed":
                    link = self._link(db, job["payload"]["link_id"])
                    if link["state"] == "importing":
                        link.update(state="failed", message=job["message"])
                        self._save_link(db, link)
                with self.guard:
                    finish_timing(job)
                    job["cancel_requested"] = False
                    self._save_job(db, job)
                    if job["kind"] in STORY_KINDS:
                        self._story_job_finished(db, job)
                    if self.cancel_events.get(job_id) is event:
                        self.cancel_events.pop(job_id, None)

    def _prepare(self, db, job):
        from .worker import prepare_manifest, verify_artifact
        payload = job["payload"]
        source = self._source(db, job["source_id"])
        manifest = SourceManifest(media_path=source["path"], **{k: payload[k] for k in
            ("rights_category", "permission_reference", "creator_credit", "contribution", "framing")})
        manifest.source_url = source.get("source_url")
        manifest_path = self.data_dir / "manifests" / (job["id"] + ".json")
        atomic_json(manifest_path, manifest.model_dump())
        if payload["mode"] == "ai":
            result = prepare_manifest(self.settings, db, manifest_path, retry=bool(job.get("retry_requested")))
            dirs = [Path(p) for p in result.get("artifacts", [])]
            if not dirs:
                state = result.get("state", "failed")
                if state == "NO_CANDIDATES":
                    job["result_message"] = ("Previously deleted clips were skipped. Choose a different cut to create a new clip." if result.get("deleted_clips") else "No suitable speech clips were found. Try manual mode or another source.")
                    return
                reason = result.get("reason")
                if reason and len(reason) < 400 and not any(x in reason.lower() for x in ("http", "token", "secret")):
                    raise WebError(reason)
                phase = {"preparing": "preparation", "transcribing": "speech transcription",
                         "selecting": "clip selection", "rendering": "video rendering",
                         "metadata": "writing clip details"}.get(result.get("stage"), "processing")
                error_type = result.get("error_type", "")
                # Only an identifier is safe to display; never expose exception text.
                detail = f" ({error_type})" if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type) else ""
                raise WebError(f"AI {phase} failed{detail}. The task needs investigation; repeating Retry may not fix it.")
        else:
            from .web_render import render_manual
            folder = self.settings.resolve(self.settings.paths.prepared) / ("manual-" + job["id"])
            clip_id = "manual-" + job["id"]
            if db.conn.execute("SELECT 1 FROM web_deleted_clips WHERE clip_id=?", (clip_id,)).fetchone():
                raise WebError("This clip was deleted. Create a new clip instead of retrying its old task.", 409)
            existing = db.conn.execute("SELECT 1 FROM publications WHERE clip_id=?", (clip_id,)).fetchone()
            if existing:
                raise WebError("This clip already has a publication record. Its existing files must be preserved.", 409)
            if db.conn.execute("SELECT 1 FROM social_publications WHERE clip_id=?", (clip_id,)).fetchone():
                raise WebError("This clip already has an Instagram or TikTok publication record. Its files must be preserved.", 409)
            self._stage(db, job, "rendering")
            rendered = render_manual(self.settings, Path(source["path"]), folder,
                start_s=float(payload["start_s"]), end_s=float(payload["end_s"]), framing=payload["framing"],
                caption_text=payload.get("caption_text", ""), on_stage=lambda stage: self._stage(db, job, stage))
            metadata = Metadata(title=payload["title"], description=payload["contribution"] + "\n\n" + payload["creator_credit"], tags=[])
            artifact = {"clip_id": clip_id, "job_id": job["id"], "source_sha256": sha256_file(Path(source["path"])),
                        "render_sha256": rendered["render_sha256"], "video_path": rendered["video_path"],
                        "caption_path": rendered.get("caption_path"), "render_spec": rendered["render_spec"],
                        "candidate": {"start_id": "manual-start", "end_id": "manual-end", "start_s": float(payload["start_s"]),
                            "end_s": float(payload["end_s"]), "hook_id": "manual-start", "payoff_id": "manual-end", "score": 0.0,
                            "reason": "Cut selected by you. Any caption overlay was entered manually.", "needs_visual_review": True},
                        "metadata": metadata.model_dump(), "source_manifest": manifest.model_dump(), "state": "REVIEW_REQUIRED",
                        "created_at": iso_now(), "web_mode": "manual", "web_is_demo": bool(source.get("is_demo"))}
            atomic_json(folder / "artifact.json", artifact)
            atomic_json(folder / "metadata.json", metadata.model_dump())
            db.conn.execute("INSERT OR REPLACE INTO jobs(id,manifest_path,state,updated_at) VALUES(?,?,?,?)",
                            (job["id"], str(manifest_path), "REVIEW_REQUIRED", iso_now()))
            db.conn.execute("INSERT OR REPLACE INTO artifacts(clip_id,job_id,artifact_dir,render_sha256,created_at) VALUES(?,?,?,?,?)",
                            (clip_id, job["id"], str(folder), rendered["render_sha256"], iso_now()))
            dirs = [folder]
        self._stage(db, job, "verifying")
        for folder in dirs:
            data = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
            verify_artifact(self.settings, folder)
            clip_id = data["clip_id"]
            db.conn.execute("INSERT OR REPLACE INTO web_clips VALUES(?,?,?)", (clip_id, source["id"], job["id"]))
            if clip_id not in job["clip_ids"]:
                job["clip_ids"].append(clip_id)

    @contextlib.contextmanager
    def mutate_clip(self, clip_id):
        from .worker import assert_editable
        with self.guard, self.db() as db:
            try:
                with self.engine_lock():
                    folder, artifact = self._artifact(db, clip_id)
                    try:
                        assert_editable(db, folder)
                    except ValueError:
                        raise WebError("This clip has a publication record. Its reviewed files and metadata must be preserved.", 409) from None
                    yield db, folder, artifact
            except Timeout:
                raise WebError("A task is using this workspace. Wait for it to finish before editing clips.", 409) from None

    def edit_clip(self, clip_id, payload):
        _strict_keys(payload, {"title", "description", "tags"})
        try:
            metadata = Metadata.model_validate(payload)
        except ValidationError:
            raise WebError("Enter a title (1–100 characters), description (1–5000 characters) and valid tags.") from None
        with self.mutate_clip(clip_id) as (db, folder, artifact):
            artifact["metadata"] = metadata.model_dump()
            artifact["source_manifest"].update(editorial_approved=False, metadata_approved=False, visual_approved=False)
            artifact["state"] = "REVIEW_REQUIRED"
            artifact.pop("approved_at", None)
            atomic_json(folder / "artifact.json", artifact)
            atomic_json(folder / "metadata.json", metadata.model_dump())
            return self._clip_record(db, artifact)

    def approve(self, clip_id, payload):
        _strict_keys(payload, {"reviewed"})
        if payload.get("reviewed") is not True:
            raise WebError("Watch the clip and confirm you reviewed its framing, content and metadata.")
        from .worker import approve_artifact
        with self.mutate_clip(clip_id) as (db, folder, artifact):
            approve_artifact(folder, True, True)
            return self._clip_record(db, json.loads((folder / "artifact.json").read_text(encoding="utf-8")))

    def media(self, kind, entity_id):
        with self.db() as db:
            if kind == "source":
                source = self._source(db, entity_id)
                if source.get("source_deleted"):
                    raise WebError("This source video was deleted. Finished clips are still available.", 404)
                path, name = Path(source["path"]), source["name"]
            else:
                _, artifact = self._artifact(db, entity_id)
                field = "video_path" if kind == "clip" else "caption_path"
                value = artifact.get(field)
                if not value:
                    raise WebError("This clip does not have a caption file.", 404)
                path = Path(value)
                name = artifact["metadata"]["title"] + (".mp4" if kind == "clip" else path.suffix)
            if not path.is_file():
                raise WebError("This video or caption file was moved or deleted.", 404)
            return path, name

    def youtube(self, action, payload):
        _strict_keys(payload, set())
        if action not in {"authorize", "poll", "revoke"}:
            raise WebError("Unknown YouTube action.", 404)
        with self.guard, self.db() as db:
            youtube_ready = bool(importlib.util.find_spec("google_auth_oauthlib") is not None
                                 and self.settings.upload.target_channel_id
                                 and self.settings.resolve(self.settings.upload.client_secrets).is_file())
            if action == "authorize" and not youtube_ready:
                raise WebError("Install the YouTube dependencies and configure your channel ID and OAuth client file first.", 409)
            return self._enqueue(db, action, {}, title={"authorize": "Connect YouTube", "poll": "Check YouTube status", "revoke": "Disconnect YouTube"}[action])

    def social_creator(self, platform):
        from .social_publish import SocialError, creator_info, provider_status
        if platform not in {"instagram", "tiktok"}:
            raise WebError("Unknown publishing platform.", 404)
        readiness = provider_status(self.settings, platform)
        if not readiness["ok"]:
            raise WebError(readiness["message"], 409)
        try:
            return creator_info(self.settings, platform)
        except SocialError as exc:
            raise WebError(str(exc), 409) from None

    def _social_setup_hash(self, platform):
        config = getattr(self.settings, platform)
        try:
            return stable_id({"settings": config.model_dump(), "credentials": sha256_file(self.settings.resolve(config.credentials_file))})
        except OSError:
            raise WebError("The platform credential file is unavailable. Check Settings and the publishing guide.", 409) from None

    def social_action(self, clip_id, platform, action, payload):
        from .social_publish import SocialError, provider_status, public_records, validate_options
        if platform not in {"instagram", "tiktok"} or action not in {"publish", "refresh", "finish"}:
            raise WebError("Unknown publishing action.", 404)
        if action == "finish" and platform != "instagram":
            raise WebError("Only an existing Instagram container can be finished.", 409)
        try:
            if action == "publish":
                options = validate_options(platform, payload)
            else:
                _strict_keys(payload, {"confirmed"} if action == "finish" else set())
                if action == "finish" and payload.get("confirmed") is not True:
                    raise WebError("Confirm you want to finish the saved Instagram publication.")
                options = dict(payload)
        except SocialError as exc:
            raise WebError(str(exc)) from None
        with self.guard, self.db() as db:
            folder, artifact = self._artifact(db, clip_id)
            record = self._clip_record(db, artifact)
            if record["is_demo"]:
                raise WebError("The synthetic sample cannot be published. Use your own authorized video.", 409)
            if artifact.get("state") != "PREPARED":
                raise WebError("Review and approve this clip before publishing.", 409)
            if not provider_status(self.settings, platform)["ok"]:
                raise WebError("Configure and enable this platform in Settings first. See the publishing setup guide.", 409)
            for row in db.conn.execute("SELECT data FROM web_jobs"):
                job = json.loads(row["data"])
                if job["state"] in ACTIVE and job.get("payload", {}).get("clip_id") == clip_id and job.get("payload", {}).get("platform") == platform:
                    raise WebError("A publishing action for this clip and platform is already queued or running.", 409)
            existing = public_records(db, clip_id).get(platform)
            if action == "publish" and existing:
                raise WebError("This platform already has a saved publication. Refresh its status or finish the existing Instagram container.", 409)
            if action != "publish" and not existing:
                raise WebError("There is no saved publication for this platform.", 409)
            if action == "finish" and not existing.get("can_finish"):
                raise WebError("Refresh Instagram status first. Only a ready, unsubmitted container can be finished.", 409)
            return self._enqueue(db, "social_" + action, {"platform": platform, "clip_id": clip_id,
                "options": options, "reviewed_artifact_hash": stable_id(artifact), "setup_hash": self._social_setup_hash(platform)},
                title={"publish": "Publish to ", "refresh": "Check ", "finish": "Finish publishing to "}[action] + platform.title())

    def _social_task(self, db, job):
        from .social_publish import SocialError, finish, publish, refresh
        payload = job["payload"]
        platform = payload["platform"]
        folder, artifact = self._artifact(db, payload["clip_id"])
        record = self._clip_record(db, artifact)
        if record["is_demo"] or artifact.get("state") != "PREPARED":
            raise WebError("The reviewed clip is unavailable or is a setup sample.", 409)
        if stable_id(artifact) != payload["reviewed_artifact_hash"] or self._social_setup_hash(platform) != payload["setup_hash"]:
            raise WebError("The clip, account settings or credentials changed after confirmation. Review the current details before submitting again.", 409)
        self._stage(db, job, "social_polling" if job["kind"] == "social_refresh" else "social_publishing")
        try:
            if job["kind"] == "social_refresh":
                result = refresh(self.settings, db, artifact["clip_id"], platform)
            elif job["kind"] == "social_finish":
                result = finish(self.settings, db, folder, platform)
            else:
                result = publish(self.settings, db, folder, platform, payload["options"])
        except SocialError as exc:
            raise WebError(str(exc), 409) from None
        job["clip_ids"] = [artifact["clip_id"]]
        job["result_message"] = result.get("message", "Platform status updated. Open the clip to review its publication details.")

    def publish(self, clip_id, payload):
        _strict_keys(payload, {"confirmed", "made_for_kids", "contains_synthetic_media", "schedule"})
        if payload.get("confirmed") is not True or any(type(payload.get(k)) is not bool for k in ("made_for_kids", "contains_synthetic_media")):
            raise WebError("Confirm publication and select the audience and synthetic media declarations.")
        from .upload import validate_schedule
        try:
            validate_schedule(payload.get("schedule"), self.settings.upload.min_lead_minutes)
        except ValueError as exc:
            raise WebError(str(exc)) from None
        with self.guard, self.db() as db:
            folder, artifact = self._artifact(db, clip_id)
            record = self._clip_record(db, artifact)
            if record["is_demo"]:
                raise WebError("The synthetic setup sample cannot be published. Use your own authorized video.", 409)
            if artifact.get("state") != "PREPARED":
                raise WebError("Review and approve this clip before publishing.", 409)
            if not self.settings.upload.enabled or not self.settings.upload.target_channel_id:
                raise WebError("Enable YouTube publishing and configure the target channel in Settings first.", 409)
            if record["publication"]:
                raise WebError("This clip already has a saved publication. Use Resume upload or refresh its publishing status.", 409)
            if any(json.loads(row["data"]).get("payload", {}).get("clip_id") == clip_id
                   and json.loads(row["data"]).get("kind") in {"publish", "resume"}
                   and json.loads(row["data"]).get("state") in ACTIVE
                   for row in db.conn.execute("SELECT data FROM web_jobs")):
                raise WebError("A YouTube upload for this clip is already queued or running.", 409)
            payload = dict(payload, clip_id=clip_id, reviewed_artifact_hash=stable_id(artifact))
            return self._enqueue(db, "publish", payload, title="Publish " + record["title"])

    def resume(self, clip_id, payload):
        _strict_keys(payload, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm that you want to resume this saved publication.")
        with self.guard, self.db() as db:
            _, artifact = self._artifact(db, clip_id)
            record = self._clip_record(db, artifact)
            if record["is_demo"]:
                raise WebError("The synthetic sample cannot be published.", 409)
            publication = db.conn.execute("SELECT * FROM publications WHERE clip_id=? AND channel_id=?",
                (clip_id, self.settings.upload.target_channel_id)).fetchone()
            if not publication:
                raise WebError("No existing publication for the configured target channel can be resumed.", 409)
            can_resume, message = self._resume_availability(artifact, dict(publication))
            if not can_resume:
                raise WebError(message, 409)
            for row in db.conn.execute("SELECT data FROM web_jobs"):
                pending = json.loads(row["data"])
                if pending["state"] in ACTIVE and pending.get("payload", {}).get("clip_id") == clip_id:
                    raise WebError("An upload action for this clip is already queued or running.", 409)
            return self._enqueue(db, "resume", {"clip_id": clip_id, "channel_id": publication["channel_id"],
                "intent_fingerprint": self._intent_fingerprint(dict(publication))}, title="Resume " + record["title"])

    def _youtube_task(self, db, job):
        from .upload import authorize, poll_publications, revoke_owner_consent, upload_prepared
        kind = job["kind"]
        self._stage(db, job, {"authorize": "authorizing", "poll": "polling", "revoke": "revoking", "publish": "publishing", "resume": "publishing"}[kind])
        if kind == "authorize":
            authorize(self.settings)
            job["result_message"] = "YouTube connected to the configured channel."
        elif kind == "poll":
            poll_publications(self.settings, db)
            job["result_message"] = "YouTube status checked. Publication details appear on your clips."
        elif kind == "revoke":
            revoke_owner_consent(self.settings, db)
            job["result_message"] = "YouTube consent revoked and local authorization removed."
        else:
            payload = job["payload"]
            folder, artifact = self._artifact(db, payload["clip_id"])
            record = self._clip_record(db, artifact)
            if record["is_demo"]:
                raise WebError("The synthetic sample cannot be published.")
            if artifact.get("state") != "PREPARED":
                raise WebError("The clip changed after publication was requested. Review and approve it again.")
            existing = db.conn.execute("SELECT * FROM publications WHERE clip_id=? AND channel_id=?",
                (artifact["clip_id"], self.settings.upload.target_channel_id)).fetchone()
            if kind == "resume":
                if (not existing or self.settings.upload.target_channel_id != payload["channel_id"]
                        or self._intent_fingerprint(dict(existing)) != payload["intent_fingerprint"]):
                    raise WebError("The saved publication changed or is unavailable. Reconcile it before resuming.", 409)
                can_resume, message = self._resume_availability(artifact, dict(existing))
                if not can_resume:
                    raise WebError(message, 409)
            elif record["publication"]:
                raise WebError("A saved publication now exists for this clip. Refresh its status or explicitly resume that publication.", 409)
            elif not existing:
                if stable_id(artifact) != payload["reviewed_artifact_hash"]:
                    raise WebError("The clip or metadata changed after publication was confirmed. Review it and submit a new publication request.", 409)
                from .upload import validate_schedule, _body
                import copy
                publication_artifact = copy.deepcopy(artifact)
                source = publication_artifact["source_manifest"]
                source.update(made_for_kids=payload["made_for_kids"], contains_synthetic_media=payload["contains_synthetic_media"],
                              schedule=validate_schedule(payload.get("schedule"), self.settings.upload.min_lead_minutes))
                # Full metadata/schedule validation before saving this review snapshot.
                _body(self.settings, publication_artifact)
            if kind == "publish" and not existing:
                result = upload_prepared(self.settings, db, folder, True, publication_options={key: source[key] for key in
                    ("made_for_kids", "contains_synthetic_media", "schedule")})
            else:
                result = upload_prepared(self.settings, db, folder, True)
            state = result.get("state", "needs_review")
            job["clip_ids"] = [artifact["clip_id"]]
            if state in {"needs_review", "needs_reconciliation", "auth_required", "retry_wait", "quota_wait", "permanent_error"}:
                raise WebError("YouTube needs attention: " + str(result.get("error") or state).replace("_", " ") + ". Check publication details before retrying.")
            job["result_message"] = "Upload accepted. Check YouTube status for processing and publication."
