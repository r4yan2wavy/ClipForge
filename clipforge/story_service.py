"""Saved, editable local story projects; uses Studio's existing queue and exports."""
from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError

from .common import Metadata, SourceManifest, atomic_json, iso_now, sha256_file, stable_id
from .job_control import checkpoint, report
from .story_engine import (GENRES, Scene, StoryError, StoryOptions, Storyboard, generate_ideas,
                           rewrite_scene, write_story)


STORY_KINDS = {"story", "story_rewrite", "story_scene", "story_ideas"}


def _error(message, status=400):
    # Deferred import avoids a service/mixin import cycle.
    from .web_service import WebError
    return WebError(message, status)


class StoryServiceMixin:
    def _story_folder(self, project_id):
        if not isinstance(project_id, str) or len(project_id) != 32 or any(c not in "0123456789abcdef" for c in project_id):
            raise _error("This story project could not be found.", 404)
        root = self.data_dir / "stories"
        folder = root / project_id
        if folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction()) or folder.resolve().parent != root.resolve():
            raise _error("This story project is outside the local project folder.", 409)
        return folder

    def _story(self, db, project_id):
        self._story_folder(project_id)
        row = db.conn.execute("SELECT data FROM web_stories WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise _error("This story project could not be found.", 404)
        return json.loads(row["data"])

    def _save_story(self, db, project):
        with self.guard:
            project["updated_at"] = iso_now()
            folder = self._story_folder(project["id"])
            atomic_json(folder / "project.json", project)
            if project.get("characters"):
                atomic_json(folder / "characters.json", project["characters"])
            db.conn.execute("INSERT OR REPLACE INTO web_stories VALUES(?,?)", (project["id"], json.dumps(project, allow_nan=False)))

    def _story_busy(self, db, project_id):
        return any(j.get("state") in {"queued", "running"} and j.get("payload", {}).get("project_id") == project_id
                   for j in (json.loads(r["data"]) for r in db.conn.execute("SELECT data FROM web_jobs")))

    def _story_public(self, db, project, full=True):
        value = {k:v for k,v in project.items() if k not in {"active_job_id"}}
        clip_id = project.get("clip_id")
        present = bool(clip_id and db.conn.execute("SELECT 1 FROM artifacts WHERE clip_id=? AND active=1", (clip_id,)).fetchone())
        value["video_url"] = f"/media/clip/{clip_id}" if present else None
        value["download_url"] = f"/media/clip/{clip_id}?download=1" if present else None
        value["export_available"] = present
        value["busy"] = self._story_busy(db, project["id"])
        if not full:
            for key in ("scenes", "characters", "provenance"):
                value.pop(key, None)
        return value

    def story_capabilities(self):
        from .story_audio import discover_voices
        voices = discover_voices(self.settings)
        renderer = bool(importlib.util.find_spec("PIL") and self.capabilities.get("ffmpeg", {}).get("ok")
                        and self.capabilities.get("ffprobe", {}).get("ok"))
        ollama = bool(self.capabilities.get("ollama", {}).get("ok"))
        missing = []
        if not renderer:
            missing.append("Install the stories extra and set up FFmpeg in Settings.")
        if not voices:
            missing.append("Install an offline Windows speech voice or a local Piper voice; see the Story Generator guide.")
        if not ollama:
            missing.append("Start Ollama with the local model selected in Settings.")
        return {"ok":bool(renderer and voices and ollama), "message":" ".join(missing) if missing else "Your local story tools are ready. Narration sets the final running time.",
                "voices":voices, "renderer_ready":renderer, "ollama_ready":ollama,
                "style":"Dark Stickman Stories", "duration_note":"Duration is a writing target; the export keeps all narration.",
                "animation_engine":"Procedural 2D animation", "hardware_note":"CPU rendering works with 720p and 1080p. Ollama uses your GPU when available."}

    def stories(self):
        capabilities = self.story_capabilities()
        with self.db() as db:
            projects = [self._story_public(db,json.loads(r["data"]),False) for r in db.conn.execute("SELECT data FROM web_stories ORDER BY rowid DESC")]
        return {"projects":projects, "capabilities":capabilities}

    def get_story(self, project_id):
        with self.db() as db:
            return {"project":self._story_public(db,self._story(db,project_id))}

    def create_story(self, payload):
        try:
            options = StoryOptions.model_validate(payload)
        except (ValidationError, ValueError):
            raise _error("Enter a story idea and choose supported duration, voice, genre and quality options.") from None
        project = {"id":uuid.uuid4().hex, "title":options.idea[:80] or "Untitled story", "state":"draft",
                   "style":"Dark Stickman Stories", "options":options.model_dump(), "characters":[], "scenes":[],
                   "created_at":iso_now(), "updated_at":iso_now(), "revision":1, "exports":[], "warnings":[]}
        with self.guard, self.db() as db:
            self._save_story(db,project)
            return {"project":self._story_public(db,project)}

    def save_story(self, project_id, payload):
        allowed = set(StoryOptions.model_fields) | {"title", "scenes"}
        if not isinstance(payload,dict) or set(payload)-allowed:
            raise _error("The project contains unsupported editing fields.")
        with self.guard, self.db() as db:
            project = self._story(db,project_id)
            if self._story_busy(db,project_id):
                raise _error("Wait for this story's current task to finish before editing it.",409)
            try:
                options = StoryOptions.model_validate({**project["options"], **{k:v for k,v in payload.items() if k in StoryOptions.model_fields}})
                title = payload.get("title",project["title"])
                if not isinstance(title,str) or not 1 <= len(title.strip()) <= 100 or any(c in title for c in "<>\r\n\x00"):
                    raise ValueError("title")
                scenes = payload.get("scenes",project["scenes"])
                if scenes:
                    plan = Storyboard.model_validate({"title":title, "characters":project["characters"], "scenes":[{k:v for k,v in scene.items() if k in Scene.model_fields} for scene in scenes]})
                    scenes = plan.model_dump()["scenes"]
                elif not isinstance(scenes,list):
                    raise ValueError("scenes")
            except (ValidationError,ValueError,TypeError,AttributeError):
                raise _error("Check the story options and scene text. Every scene must use a character from this project's library.") from None
            if options.model_dump() != project["options"] or scenes != project["scenes"] or title != project["title"]:
                project.update(options=options.model_dump(), title=title.strip(), scenes=scenes, state="draft", error=None,
                               revision=project["revision"]+1, warnings=[])
            self._save_story(db,project)
            return {"project":self._story_public(db,project)}

    def story_action(self, project_id, action, payload, scene_id=None):
        if payload or action not in {"generate","rewrite","scene"}:
            raise _error("This story action does not accept extra options.")
        with self.guard, self.db() as db:
            project=self._story(db,project_id)
            if self._story_busy(db,project_id):
                raise _error("This story already has a task in the workshop.",409)
            if scene_id and not any(s["id"]==scene_id for s in project["scenes"]):
                raise _error("This scene could not be found.",404)
            caps = self.story_capabilities()
            if (action != "generate" or not project["scenes"]) and not caps["ollama_ready"]:
                raise _error("Start Ollama and select an installed local model in Settings.",409)
            if action=="generate":
                if not caps["renderer_ready"] or not caps["voices"]:
                    raise _error(caps["message"],409)
                if not project["options"]["voice"]:
                    project["options"]["voice"]=caps["voices"][0]["id"]
                if project["options"]["voice"] not in {v["id"] for v in caps["voices"]}:
                    raise _error("The selected voice is unavailable. Choose an installed narrator and save the project.",409)
            kind={"generate":"story", "rewrite":"story_rewrite", "scene":"story_scene"}[action]
            result=self._enqueue(db,kind,{"project_id":project_id,"scene_id":scene_id},title=project["title"])
            project.update(state="queued",error=None,active_job_id=result["job"]["id"])
            self._save_story(db,project)
            return result

    def story_ideas(self,payload):
        if not isinstance(payload,dict) or set(payload)-{"genre","idea"} or payload.get("genre","random") not in GENRES or not isinstance(payload.get("idea",""),str) or len(payload.get("idea",""))>3000:
            raise _error("Choose a supported genre and a short inspiration idea.")
        if not self.capabilities.get("ollama",{}).get("ok"):
            raise _error("Start your selected local Ollama model to generate ideas.",409)
        with self.guard,self.db() as db:
            return self._enqueue(db,"story_ideas",payload,title="Five original story ideas")

    def story_batch(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"options", "count"}
                or type(payload["count"]) is not int or not 1 <= payload["count"] <= 5):
            raise _error("Choose between one and five stories for this batch.")
        try:
            options = StoryOptions.model_validate(payload["options"])
        except (ValidationError, ValueError):
            raise _error("Check the story options before generating a batch.") from None
        with self.guard, self.db() as db:
            caps = self.story_capabilities()
            if not caps["ok"]:
                raise _error(caps["message"], 409)
            if not options.voice:
                options.voice = caps["voices"][0]["id"]
            if options.voice not in {v["id"] for v in caps["voices"]}:
                raise _error("Choose an installed local narrator before generating a batch.", 409)
            active = sum(json.loads(r["data"])["state"] in {"queued", "running"}
                         for r in db.conn.execute("SELECT data FROM web_jobs"))
            if active + payload["count"] > 20:
                raise _error("There is not enough room in the local queue for this batch. Let current tasks finish first.", 409)
            projects, jobs = [], []
            for _ in range(payload["count"]):
                project = self.create_story(options.model_dump())["project"]
                job = self.story_action(project["id"], "generate", {})["job"]
                projects.append(self.get_story(project["id"])["project"])
                jobs.append(job)
            return {"projects": projects, "jobs": jobs}

    def _story_task(self,db,job):
        if job["kind"]=="story_ideas":
            job["result"]=generate_ideas(self.settings,job["payload"].get("genre","random"),job["payload"].get("idea",""))
            job["result_message"]="Five new story ideas are ready in AI Story Generator."
            return
        project=self._story(db,job["payload"]["project_id"])
        if job["kind"]=="story_scene":
            project.update(state="writing")
            self._save_story(db,project)
            scene,provenance=rewrite_scene(self.settings,project,job["payload"]["scene_id"])
            project["scenes"]=[scene if s["id"]==scene["id"] else s for s in project["scenes"]]
            project.update(state="draft",provenance=provenance,revision=project["revision"]+1)
            self._save_story(db,project)
            job["result_message"]="The scene was rewritten. Review the storyboard and choose Generate video for a new export."
            return
        if not project["scenes"] or job["kind"]=="story_rewrite":
            project.update(state="writing")
            self._save_story(db,project)
            plan,provenance=write_story(self.settings,project["options"])
            project.update(**plan,provenance=provenance,revision=project["revision"]+1,state="draft")
            self._save_story(db,project)
        if job["kind"]=="story_rewrite":
            job["result_message"]="Your new script and storyboard are saved. Review them or choose Generate video."
            return
        self._render_story_project(db,job,project)

    def _render_story_project(self,db,job,project):
        from .story_audio import synthesize_story
        from .story_animation import render_story
        from .worker import verify_artifact
        clip_id="story-"+job["id"]
        folder=self.settings.resolve(self.settings.paths.prepared)/clip_id
        if db.conn.execute("SELECT 1 FROM web_deleted_clips WHERE clip_id=?",(clip_id,)).fetchone():
            raise StoryError("This export was deleted. Generate a new version from the saved story project.")
        if db.conn.execute("SELECT 1 FROM artifacts WHERE clip_id=?",(clip_id,)).fetchone():
            job["clip_ids"]=[clip_id]
            return
        root=self._story_folder(project["id"])
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="attempt-",dir=root) as temporary:
            staging=Path(temporary)
            project.update(state="narrating")
            self._save_story(db,project)
            audio=synthesize_story(project,staging,self.settings)
            checkpoint()
            timed={**project,"scenes":audio["scenes"],"duration_s":audio["duration_s"]}
            project.update(state="rendering")
            self._save_story(db,project)
            try:
                rendered=render_story(timed,Path(audio["audio_path"]),staging/"clip.mp4",self.settings)
            except ValueError as error:
                raise StoryError(str(error)) from None
            report("verifying")
            checkpoint()
            folder.mkdir(parents=True,exist_ok=True)
            registered=False
            try:
                shutil.move(str(staging/"clip.mp4"),str(folder/"clip.mp4"))
                atomic_json(folder/"storyboard.json",timed)
                atomic_json(folder/"characters.json",project["characters"])
                caption=folder/"captions.srt" if project["options"]["captions"] else None
                if caption:
                    caption.write_text(self._story_srt(audio["scenes"]),encoding="utf-8")
                tags=["story", "animation", project["options"]["genre"].replace("_"," ")]
                metadata=Metadata(title=project["title"],description=("An original fictional animated story made locally with ClipForge.\n\n"+" ".join(s["narration"] for s in project["scenes"]))[:5000],tags=tags)
                manifest=SourceManifest(media_path=str(folder/"storyboard.json"),rights_category="original",
                    permission_reference="Locally generated original fiction and procedural vector animation.",
                    creator_credit="Created with ClipForge AI Story Generator",contribution="Original script, consistent characters, local narration, animation and original synthesized soundtrack.",framing="fit")
                spec=self.settings.render.model_dump()
                spec.update(width=rendered["width"],height=rendered["height"],fps=rendered["fps"])
                artifact={"clip_id":clip_id,"job_id":job["id"],"source_sha256":stable_id(timed),
                    "render_sha256":sha256_file(folder/"clip.mp4"),"video_path":str(folder/"clip.mp4"),
                    "caption_path":str(caption) if caption else None,"render_spec":{"render":spec},
                    "candidate":{"start_id":"story-start","end_id":"story-end","start_s":0.0,"end_s":float(audio["duration_s"]),
                        "hook_id":"story-start","payoff_id":"story-end","score":0.0,"reason":"Original fictional story generated with local AI and reusable animated characters.","needs_visual_review":True},
                    "metadata":metadata.model_dump(),"source_manifest":manifest.model_dump(),"state":"REVIEW_REQUIRED",
                    "created_at":iso_now(),"web_mode":"story","story_project_id":project["id"]}
                atomic_json(folder/"artifact.json",artifact)
                atomic_json(folder/"metadata.json",metadata.model_dump())
                config=self.settings.model_copy(deep=True)
                config.render.width,config.render.height=rendered["width"],rendered["height"]
                verify_artifact(config,folder)
                checkpoint()
                with db.transaction():
                    db.conn.execute("INSERT INTO jobs(id,manifest_path,state,updated_at) VALUES(?,?,?,?)",(job["id"],str(root/"project.json"),"REVIEW_REQUIRED",iso_now()))
                    db.conn.execute("INSERT INTO artifacts(clip_id,job_id,artifact_dir,render_sha256,created_at) VALUES(?,?,?,?,?)",(clip_id,job["id"],str(folder),artifact["render_sha256"],iso_now()))
                    db.conn.execute("INSERT INTO web_clips VALUES(?,?,?)",(clip_id,None,job["id"]))
                registered=True
                job["clip_ids"]=[clip_id]
                warnings=list(audio.get("warnings",[]))
                project.update(state="completed",clip_id=clip_id,duration_s=audio["duration_s"],warnings=warnings,
                    scenes=audio["scenes"],error=None)
                project["exports"].append({"clip_id":clip_id,"created_at":iso_now(),"revision":project["revision"]})
                self._save_story(db,project)
                job["result_message"]="Your animated story is ready. Preview it in AI Story Generator or review and export it from Your clips."
            finally:
                if not registered and folder.exists():
                    shutil.rmtree(folder)

    @staticmethod
    def _story_srt(scenes):
        def stamp(value):
            ms=round(value*1000)
            return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"
        cues=[]
        for scene in scenes:
            words=scene.get("words",[])
            if words:
                pending=[]
                for word in words:
                    if word.get("timing") == "phrase":
                        if pending:
                            cues.append((pending[0]["start_s"],pending[-1]["end_s"]," ".join(w["text"] for w in pending)))
                            pending=[]
                        cues.append((word["start_s"],word["end_s"],word["text"]))
                    else:
                        pending.append(word)
                        if len(pending)==5:
                            cues.append((pending[0]["start_s"],pending[-1]["end_s"]," ".join(w["text"] for w in pending)))
                            pending=[]
                if pending:
                    cues.append((pending[0]["start_s"],pending[-1]["end_s"]," ".join(w["text"] for w in pending)))
            else:
                cues.append((scene["start_s"],scene["start_s"]+scene["duration_s"],scene["narration"]))
        return "\n\n".join(f"{i+1}\n{stamp(a)} --> {stamp(b)}\n{text}" for i,(a,b,text) in enumerate(cues))+"\n"

    def _story_job_finished(self,db,job):
        project_id=job.get("payload",{}).get("project_id")
        if not project_id:
            return
        project=self._story(db,project_id)
        if job["state"] in {"failed","cancelled","interrupted"}:
            project.update(state=job["state"],error=job["message"])
        project.pop("active_job_id",None)
        self._save_story(db,project)
