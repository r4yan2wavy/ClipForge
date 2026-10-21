"""Local narrator setup and fixed speech previews through the Studio queue."""
from __future__ import annotations

import shutil
import tempfile
import json
from pathlib import Path


VOICE_KINDS = {"story_voice_install", "story_voice_preview"}


def _error(message, status=400):
    from .web_service import WebError
    return WebError(message, status)


class StoryVoiceServiceMixin:
    def story_voices(self, refresh=False):
        from .story_voices import voice_library
        if refresh:
            from .story_audio import refresh_voices
            refresh_voices(self.settings)
        return voice_library(self.settings)

    def story_voice_action(self, action, payload):
        from .story_audio import discover_voices
        if action not in {"install", "preview"}:
            raise _error("This voice action could not be found.", 404)
        field = "id" if action == "install" else "voice"
        if (not isinstance(payload, dict) or set(payload) != {field}
                or not isinstance(payload[field], str) or not 1 <= len(payload[field]) <= 240):
            raise _error("Choose a voice from the local voice library.")
        with self.guard, self.db() as db:
            identifier = payload[field]
            if action == "install":
                library = self.story_voices()
                voice = next((v for v in library["catalog"] if v["id"] == identifier), None)
                if not voice:
                    raise _error("Choose a supported voice pack from the library.")
                if not library["engine_ready"]:
                    raise _error(library["engine_message"], 409)
                title = "Install voice: " + voice["name"]
            else:
                voice = next((v for v in discover_voices(self.settings) if v["id"] == identifier), None)
                if not voice:
                    raise _error("That voice is unavailable. Refresh your installed voices and choose again.", 409)
                title = "Preview voice: " + voice["name"]
            kind = "story_voice_" + action
            for row in db.conn.execute("SELECT data FROM web_jobs"):
                job = json.loads(row["data"])
                if (job["kind"] == kind and job["state"] in {"queued", "running"}
                        and job.get("payload", {}).get(field) == identifier):
                    return {"job": self.job_record(job, db)}
            return self._enqueue(db, kind, dict(payload), title=title)

    def _voice_preview_folder(self, job_id):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise _error("This voice preview could not be found.", 404)
        root = self.data_dir / "voice-previews"
        folder = root / job_id
        if (root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction())
                or folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction())
                or folder.resolve().parent != root.resolve()):
            raise _error("This voice preview folder is unavailable.", 409)
        return folder

    def _story_voice_task(self, db, job):
        from .story_voices import install_voice, preview_voice
        from .job_control import checkpoint
        if job["kind"] == "story_voice_install":
            install_voice(job["payload"]["id"], self.settings)
            job["result_message"] = "Your voice is installed. Choose it from Local narrator."
            return
        folder = self._voice_preview_folder(job["id"])
        folder.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="preview-", dir=folder.parent) as temporary:
            staging = Path(temporary)
            path = Path(preview_voice(job["payload"]["voice"], self.settings, staging))
            checkpoint()
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(staging.resolve())
                    or path.stat().st_size > 8 * 1024 * 1024):
                raise _error("The selected narrator did not create a usable voice preview.")
            folder.mkdir(exist_ok=False)
            try:
                shutil.copyfile(path, folder / "preview.wav")
                checkpoint()
            except BaseException:
                shutil.rmtree(folder)
                raise
        job["voice_preview_url"] = "/media/story-voice/" + job["id"]
        job["result"] = {"voice_preview_url": job["voice_preview_url"]}
        job["result_message"] = "Your voice preview is ready. Play it in the voice library."

    def story_voice_media(self, job_id):
        with self.db() as db:
            job = self._job(db, job_id)
            if job["kind"] != "story_voice_preview" or job["state"] != "completed":
                raise _error("This voice preview is not ready.", 404)
            path = self._voice_preview_folder(job_id) / "preview.wav"
            if not path.is_file() or path.is_symlink():
                raise _error("This voice preview is no longer available. Preview the voice again.", 404)
            return path, "voice-preview.wav"

    def _delete_voice_preview(self, job):
        if job["kind"] != "story_voice_preview":
            return
        folder = self._voice_preview_folder(job["id"])
        if folder.exists():
            try:
                shutil.rmtree(folder)
            except OSError:
                raise _error("Close this voice preview before deleting its workshop item.", 409) from None
