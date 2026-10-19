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


class WebService:
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
            """)
            for row in db.conn.execute("SELECT * FROM web_jobs").fetchall():
                job = json.loads(row["data"])
                if job["state"] in ACTIVE:
                    job.update(state="interrupted", stage="interrupted", message="ClipForge stopped before this task finished. Retry when ready.")
                    self._save_job(db, job)
            for row in db.conn.execute("SELECT * FROM web_links").fetchall():
                link = json.loads(row["data"])
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

    @staticmethod
    def source_record(source):
        return {k: v for k, v in source.items() if k not in {"path", "sha256"}}

    def _save_job(self, db, job):
        db.conn.execute("INSERT OR REPLACE INTO web_jobs VALUES(?,?)", (job["id"], json.dumps(job, allow_nan=False)))

    def _job(self, db, job_id):
        row = db.conn.execute("SELECT data FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise WebError("This task could not be found.", 404)
        return json.loads(row["data"])

    def job_record(self, job, db):
        record = {k: v for k, v in job.items() if k not in {"payload", "engine_id", "kind", "retry_requested"}}
        if job.get("kind", "").startswith("social_"):
            record["platform"] = job["payload"]["platform"]
            record["target_clip_id"] = job["payload"]["clip_id"]
        if job["state"] == "running" and job.get("engine_id"):
            row = db.conn.execute("SELECT state FROM jobs WHERE id=?", (job["engine_id"],)).fetchone()
            if row and row["state"] in {"TRANSCRIBING", "SELECTING", "RENDERING"}:
                stage = row["state"].lower()
                record.update(stage=stage, progress={"transcribing": 20, "selecting": 45, "rendering": 65}[stage],
                              message={"transcribing": "Transcribing speech locally…", "selecting": "Finding clips with your local model…", "rendering": "Rendering vertical clips…"}[stage])
        return record

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
                "publication": _public(dict(publication)) if publication else None}

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
            sources = [self.source_record(s) for s in self._sources(db)]
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
        return {key: value for key, value in link.items() if key not in {"private_url", "clip_id"}}

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
            ready = descriptor["platform"] == "direct" or (descriptor["clip_id"] and twitch_status(self.settings)["ok"])
            if ready:
                link.update(state="importing", message="Downloading the authorized video into your local library…")
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
            if not Path(source["path"]).is_file():
                raise WebError("The attached video was moved or deleted. Add it again.", 409)
            source["source_url"] = link["url"]
            db.conn.execute("UPDATE web_sources SET data=? WHERE id=?", (json.dumps(source), source["id"]))
            link.update(state="ready", source_id=source["id"], message="Your video file is attached and ready to edit.")
            self._save_link(db, link)
            return {"link": self.link_record(link), "source": self.source_record(source)}

    def retry_link(self, link_id, payload):
        from .link_import import twitch_status
        _strict_keys(payload, set())
        with self.guard, self.db() as db:
            link = self._link(db, link_id)
            if link["state"] not in {"failed", "needs_file"} or not link["can_download"]:
                raise WebError("This link cannot be downloaded. Attach an authorized video file instead.", 409)
            if link["platform"] == "twitch" and not twitch_status(self.settings)["ok"]:
                raise WebError("Configure current Twitch broadcaster/editor credentials in Settings first.", 409)
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
        _strict_keys(payload, {"confirmed"})
        if payload.get("confirmed") is not True:
            raise WebError("Confirm you want to delete this saved link.")
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
            # The saved reference is independent of sources, files and artifacts.
            # Source provenance and publication history must remain intact.
            db.conn.execute("DELETE FROM web_links WHERE id=?", (link_id,))
            return {"deleted": link_id}

    def _import_link_task(self, db, job):
        from .link_import import LinkError, VIDEO_EXTENSIONS, download_video, twitch_download
        import urllib.parse
        link = self._link(db, job["payload"]["link_id"])
        target = None
        try:
            if link.get("source_id"):
                job["result_message"] = "This source is already available in your library."
                return
            url = link.get("private_url", link["url"])
            name = "Imported video"
            if link["platform"] == "twitch":
                url, name = twitch_download(self.settings, link["clip_id"])
            extension = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.lower()
            if extension not in VIDEO_EXTENSIONS:
                extension = ".mp4"
            target = self.data_dir / "sources" / (uuid.uuid4().hex + extension)
            self._stage(db, job, "downloading")
            download_video(url, target, twitch=link["platform"] == "twitch", cancelled=self.stop.is_set)
            source = self.register_source(target, name + extension)
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
            message = str(exc) if isinstance(exc, (LinkError, WebError)) else "The video import could not finish. Check the link and local tools, then retry explicitly."
            link.update(state="failed", message=message)
            self._save_link(db, link)
            raise WebError(message) from None

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
        return any(json.loads(r["data"])["state"] in ACTIVE for r in db.conn.execute("SELECT data FROM web_jobs"))

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
            if not Path(source["path"]).is_file():
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
               "source_name": source["name"] if source else ("Video link" if kind == "link_import" else payload.get("platform", "YouTube").title()),
               "title": title or kind.replace("_", " ").title(), "mode": payload.get("mode", kind),
               "state": "queued", "stage": "queued", "message": "Waiting for the local worker…", "progress": 0,
               "created_at": iso_now(), "clip_ids": []}
        if kind == "clip" and payload.get("mode") == "ai":
            job["engine_id"] = stable_id(str((self.data_dir / "manifests" / (job["id"] + ".json")).resolve()))
        self._save_job(db, job)
        self.pending.put(job["id"])
        return {"job": self.job_record(job, db)}

    def retry(self, job_id):
        with self.guard, self.db() as db:
            job = self._job(db, job_id)
            if job["state"] not in {"failed", "interrupted"}:
                raise WebError("Only failed or interrupted tasks can be retried.", 409)
            if job["kind"] != "clip":
                raise WebError("Use the saved link or platform publishing controls to continue this task explicitly.", 409)
            job.update(state="queued", stage="queued", message="Waiting to retry…", progress=0)
            job["retry_requested"] = True
            self._save_job(db, job)
            self.pending.put(job_id)
            return {"job": self.job_record(job, db)}

    def _stage(self, db, job, stage):
        messages = {"preparing": "Checking the source and local tools…", "rendering": "Rendering your vertical clip…",
                    "verifying": "Checking the rendered clip…", "authorizing": "Complete YouTube consent in the browser window…",
                    "publishing": "Sending the reviewed clip to YouTube…", "polling": "Checking YouTube processing status…", "revoking": "Disconnecting YouTube…",
                    "downloading": "Downloading the authorized video into your local library…",
                    "social_publishing": "Sending the reviewed clip to the selected platform…",
                    "social_polling": "Checking the selected platform's processing status…"}
        job.update(stage=stage, message=messages.get(stage, stage.title()), progress={"preparing": 5, "rendering": 40, "verifying": 90}.get(stage, 20))
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
                job = self._job(db, job_id)
                try:
                    with self.engine_lock():
                        job.update(state="running")
                        self._stage(db, job, "preparing")
                        from .upload import expire_owner_observations
                        from .sourcing import purge
                        purge(db, self.settings.discovery.retention_days)
                        expire_owner_observations(db)
                        if job["kind"] == "clip":
                            self._prepare(db, job)
                        elif job["kind"] == "link_import":
                            self._import_link_task(db, job)
                        elif job["kind"].startswith("social_"):
                            self._social_task(db, job)
                        else:
                            self._youtube_task(db, job)
                        job.update(state="completed", stage="ready", progress=100,
                                   message=job.get("result_message", "Ready for your review."))
                except Timeout:
                    job.update(state="failed", stage="failed", message="Another ClipForge command is running. Retry when it finishes.")
                except WebError as exc:
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
                self._save_job(db, job)

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
                    job["result_message"] = "No suitable speech clips were found. Try manual mode or another source."
                    return
                reason = result.get("reason")
                if reason and len(reason) < 400 and not any(x in reason.lower() for x in ("http", "token", "secret")):
                    raise WebError(reason)
                raise WebError("AI processing needs a complete local Whisper model, running Ollama model and working FFmpeg. Check Settings, then retry.")
        else:
            from .web_render import render_manual
            folder = self.settings.resolve(self.settings.paths.prepared) / ("manual-" + job["id"])
            clip_id = "manual-" + job["id"]
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
