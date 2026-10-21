"""Local story projects, queue lifecycles and review-required clip exports."""
from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_web
from clipforge.common import iso_now
from clipforge.job_control import checkpoint
from clipforge.story_engine import Storyboard
from clipforge.story_audio import StoryAudioError
from clipforge.web_service import WebError, WebService


def plan():
    return Storyboard.model_validate({
        "title": "The Other Window",
        "characters": [{"id": "boy", "name": "Alex", "color": "#fbfbff", "accessory": "scarf"},
                       {"id": "shadow", "name": "The shadow", "color": "#9f86c0", "accessory": "none"}],
        "scenes": [{"narration": "My window opened into another world.", "setting": "bedroom", "action": "look", "character_ids": ["boy"], "camera": "zoom", "lighting": "normal", "effect": "none"},
                   {"narration": "A shadow waved back with my hand.", "setting": "hallway", "action": "point", "character_ids": ["boy", "shadow"], "camera": "pan", "lighting": "flicker", "effect": "shake"}]
    }).model_dump()


class StoryServiceTest(unittest.TestCase):
    setUp = test_web.StudioTest.setUp
    tearDown = test_web.StudioTest.tearDown
    wait_job = test_web.StudioTest.wait_job

    @staticmethod
    def capabilities():
        return {"ok": True, "renderer_ready": True, "ollama_ready": True,
                "voices": [{"id": "sapi:Test Voice", "name": "Test Voice", "provider": "Windows offline"}],
                "message": "Local story tools are ready."}

    def project(self, with_plan=False):
        value = self.service.create_story({"idea": "A bedroom window shows a different world every night.",
                                           "duration": 30, "quality": 720, "voice": "sapi:Test Voice"})["project"]
        if with_plan:
            with self.service.db() as db:
                stored = self.service._story(db, value["id"])
                stored.update(**plan())
                self.service._save_story(db, stored)
            value = self.service.get_story(value["id"])["project"]
        return value

    @staticmethod
    def audio(project, folder, settings):
        scenes = copy.deepcopy(project["scenes"])
        for i, scene in enumerate(scenes):
            scene.update(start_s=i * 2.0, duration_s=2.0,
                         words=[{"text": scene["narration"], "start_s": i * 2.0, "end_s": (i + 1) * 2.0, "timing": "phrase"}])
        path = Path(folder) / "story-audio.wav"
        path.write_bytes(b"synthetic narration fixture")
        return {"audio_path": path, "scenes": scenes, "duration_s": 4.0,
                "warnings": ["A fixture voice uses phrase captions."]}

    @staticmethod
    def renderer(project, audio_path, output_path, settings):
        assert audio_path.is_file()
        output_path.write_bytes(b"rendered synthetic story fixture")
        return {"width": 720, "height": 1280, "fps": 24, "duration_s": project["duration_s"]}

    def render_project(self, project=None):
        project = project or self.project()
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_service.write_story", return_value=(plan(), {"model": "local-fixture", "digest": "verified-fixture"})), \
             patch("clipforge.story_audio.synthesize_story", side_effect=self.audio), \
             patch("clipforge.story_animation.render_story", side_effect=self.renderer), \
             patch("clipforge.worker.verify_artifact", return_value={}) as verified:
            job = self.service.story_action(project["id"], "generate", {})["job"]
            result = self.wait_job(job["id"])
            self.assertEqual(result["state"], "completed", result)
            self.assertEqual(verified.call_count, 1)
        return self.service.get_story(project["id"])["project"], result

    def test_project_draft_and_edits_survive_restart_without_exposing_filesystem_paths(self):
        project = self.project()
        self.assertEqual(project["state"], "draft")
        self.assertEqual(project["scenes"], [])
        saved = self.service.save_story(project["id"], {"title": "A Different Window", "genre": "suspense", "music": False})["project"]
        self.assertEqual(saved["revision"], 2)
        self.assertFalse(saved["options"]["music"])
        folder = self.service._story_folder(project["id"])
        self.assertEqual(json.loads((folder / "project.json").read_text())["title"], saved["title"])
        self.assertNotIn("active_job_id", saved)
        self.assertNotIn("path", saved)
        self.service.close()
        self.service = WebService(self.config)
        restored = self.service.get_story(project["id"])["project"]
        self.assertEqual(restored["title"], "A Different Window")
        self.assertEqual(restored["options"]["genre"], "suspense")
        self.assertFalse(restored["export_available"])
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()):
            listing = self.service.stories()
        self.assertEqual(len(listing["projects"]), 1)
        self.assertNotIn("scenes", listing["projects"][0])

    def test_validation_rejects_invalid_options_titles_and_project_ids(self):
        for payload in ({"duration": 31}, {"quality": 480}, {"genre": "copied_video"},
                        {"idea": "bad\x00text"}, {"file": "outside.txt"}):
            with self.subTest(payload=payload), self.assertRaises(WebError):
                self.service.create_story(payload)
        project = self.project()
        for payload in ({"title": ""}, {"title": "<bad>"}, {"title": "two\nlines"},
                        {"characters": []}, {"scenes": "not a scene list"}, {"music": []}):
            with self.subTest(payload=payload), self.assertRaises(WebError):
                self.service.save_story(project["id"], payload)
        for identifier in ("../outside", "x" * 32, "0123456789abcdef" * 2):
            with self.subTest(identifier=identifier), self.assertRaises(WebError):
                self.service.get_story(identifier)
        self.assertEqual(self.service.get_story(project["id"])["project"]["revision"], 1)

    def test_scene_edits_enforce_the_persistent_character_library(self):
        project = self.project(with_plan=True)
        scenes = copy.deepcopy(project["scenes"])
        scenes[1]["character_ids"] = ["unrelated_character"]
        with self.assertRaises(WebError):
            self.service.save_story(project["id"], {"scenes": scenes})
        unchanged = self.service.get_story(project["id"])["project"]
        self.assertEqual(unchanged["scenes"], project["scenes"])
        scenes[1]["character_ids"] = ["shadow"]
        scenes[1]["narration"] = "The shadow whispered through the locked window."
        saved = self.service.save_story(project["id"], {"scenes": scenes})["project"]
        self.assertEqual(saved["characters"], project["characters"])
        self.assertEqual(saved["scenes"][1]["character_ids"], ["shadow"])
        self.assertTrue((self.service._story_folder(project["id"]) / "characters.json").is_file())

    def test_successful_pipeline_exports_regular_review_required_clip_and_edit_keeps_old_export(self):
        project, job = self.render_project()
        self.assertEqual(project["state"], "completed")
        self.assertTrue(project["export_available"])
        self.assertEqual(project["duration_s"], 4)
        self.assertEqual(len(project["exports"]), 1)
        self.assertEqual(job["clip_ids"], [project["clip_id"]])
        clip = self.service.get_clip(project["clip_id"])
        self.assertEqual(clip["state"], "REVIEW_REQUIRED")
        self.assertEqual(clip["mode"], "story")
        self.assertEqual((clip["width"], clip["height"]), (720, 1280))
        self.assertFalse(clip["submitted"])
        self.assertTrue(clip["caption_url"])
        self.assertEqual(self.service.library()["sources"], [])
        path, _ = self.service.media("clip", project["clip_id"])
        prior_bytes = path.read_bytes()
        with self.service.db() as db:
            folder, artifact = self.service._artifact(db, clip["id"])
            self.assertTrue(artifact["candidate"]["needs_visual_review"])
            self.assertFalse(artifact["source_manifest"]["editorial_approved"])
            self.assertTrue((folder / "storyboard.json").is_file())
            self.assertTrue((folder / "characters.json").is_file())
            self.assertIn("00:00:02,000", (folder / "captions.srt").read_text())
        edited = self.service.save_story(project["id"], {"title": "An Edited Story"})["project"]
        self.assertEqual(edited["state"], "draft")
        self.assertTrue(edited["export_available"])
        self.assertEqual(path.read_bytes(), prior_bytes)
        self.assertEqual(self.service.get_clip(clip["id"])["title"], "The Other Window")
        self.assertEqual(edited["exports"], project["exports"])

    def test_render_failure_does_not_register_or_replace_completed_export(self):
        project, _ = self.render_project()
        original = self.service.media("clip", project["clip_id"])[0]
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_audio.synthesize_story", side_effect=self.audio), \
             patch("clipforge.story_animation.render_story", side_effect=ValueError("synthetic failure")):
            job = self.service.story_action(project["id"], "generate", {})["job"]
            failed = self.wait_job(job["id"])
        self.assertEqual(failed["state"], "failed")
        saved = self.service.get_story(project["id"])["project"]
        self.assertEqual(saved["state"], "failed")
        self.assertTrue(saved["export_available"])
        self.assertTrue(original.is_file())
        self.assertEqual(len(saved["exports"]), 1)
        self.assertFalse((self.service.settings.resolve("prepared") / ("story-" + job["id"])).exists())
        self.assertEqual(list(self.service._story_folder(project["id"]).glob("attempt-*")), [])

    def test_cancel_queued_and_running_story_then_retry_uses_new_export_job(self):
        first, second = self.project(with_plan=True), self.project(with_plan=True)
        entered = threading.Event()

        def narrate(project, folder, settings):
            (folder / "partial-narration.wav").write_bytes(b"unfinished local audio")
            entered.set()
            while True:
                checkpoint()
                time.sleep(.01)

        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_audio.synthesize_story", side_effect=narrate):
            running = self.service.story_action(first["id"], "generate", {})["job"]
            self.assertTrue(entered.wait(2))
            queued = self.service.story_action(second["id"], "generate", {})["job"]
            self.assertTrue(queued["can_cancel"])
            with self.assertRaises(WebError):
                self.service.save_story(first["id"], {"title": "Race edit"})
            with self.assertRaises(WebError):
                self.service.story_action(first["id"], "rewrite", {})
            self.service.cancel_job(queued["id"], {"confirmed": True})
            self.assertEqual(self.service.get_story(second["id"])["project"]["state"], "cancelled")
            self.service.delete_job(queued["id"], {"confirmed": True})
            self.service.cancel_job(running["id"], {"confirmed": True})
            self.assertEqual(self.wait_job(running["id"])["state"], "cancelled")
        stopped = self.service.get_story(first["id"])["project"]
        self.assertEqual(stopped["state"], "cancelled")
        self.assertFalse(stopped["busy"])
        self.assertEqual(stopped["characters"], first["characters"])
        self.assertEqual(list(self.service._story_folder(first["id"]).glob("attempt-*")), [])
        self.assertTrue(self.service.thread.is_alive())
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_audio.synthesize_story", side_effect=self.audio), \
             patch("clipforge.story_animation.render_story", side_effect=self.renderer), \
             patch("clipforge.worker.verify_artifact", return_value={}):
            retried = self.service.retry(running["id"])["job"]
            self.assertNotEqual(retried["id"], running["id"])
            self.assertEqual(self.wait_job(retried["id"])["state"], "completed")
        self.assertEqual(self.service.get_story(first["id"])["project"]["state"], "completed")

    def test_rewrite_one_scene_preserves_characters_other_scene_and_prior_export(self):
        project, _ = self.render_project()
        replacement = {k: v for k, v in project["scenes"][1].items() if k in plan()["scenes"][1]}
        replacement["narration"] = "Then the shadow closed the window from inside my room."
        original = self.service.media("clip", project["clip_id"])[0]
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_service.rewrite_scene", return_value=(replacement, {"model": "local-fixture", "digest": "next"})) as rewritten:
            job = self.service.story_action(project["id"], "scene", {}, project["scenes"][1]["id"])["job"]
            self.assertEqual(self.wait_job(job["id"])["state"], "completed")
        rewritten.assert_called_once()
        saved = self.service.get_story(project["id"])["project"]
        self.assertEqual(saved["state"], "draft")
        self.assertEqual(saved["characters"], project["characters"])
        self.assertEqual(saved["scenes"][0], project["scenes"][0])
        self.assertEqual(saved["scenes"][1]["narration"], replacement["narration"])
        self.assertEqual(saved["exports"], project["exports"])
        self.assertTrue(saved["export_available"])
        self.assertTrue(original.is_file())

    def test_deleted_story_export_stays_deleted_while_project_can_generate_another(self):
        project, _ = self.render_project()
        self.service.delete_clip(project["clip_id"], {"confirmed": True})
        saved = self.service.get_story(project["id"])["project"]
        self.assertFalse(saved["export_available"])
        self.assertIsNone(saved["video_url"])
        self.assertEqual(saved["scenes"], project["scenes"])
        again, _ = self.render_project(saved)
        self.assertTrue(again["export_available"])
        self.assertNotEqual(again["clip_id"], project["clip_id"])

    def test_interrupted_project_state_and_storyboard_are_restored_on_restart(self):
        project = self.project(with_plan=True)
        job_id = "a" * 32
        with self.service.db() as db:
            stored = self.service._story(db, project["id"])
            stored.update(state="rendering", active_job_id=job_id)
            self.service._save_story(db, stored)
            self.service._save_job(db, {"id": job_id, "kind": "story", "payload": {"project_id": project["id"]},
                                       "state": "running", "stage": "story_rendering", "created_at": iso_now(), "clip_ids": []})
        self.service.close()
        self.service = WebService(self.config)
        restored = self.service.get_story(project["id"])["project"]
        self.assertEqual(restored["state"], "interrupted")
        self.assertFalse(restored["busy"])
        self.assertEqual(restored["characters"], project["characters"])
        self.assertEqual(restored["scenes"], project["scenes"])
        self.assertNotIn("active_job_id", restored)

    def test_batch_validates_all_inputs_and_capacity_before_creating_any_project(self):
        def project_count():
            with self.service.db() as db:
                return db.conn.execute("SELECT COUNT(*) FROM web_stories").fetchone()[0]

        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()):
            invalid = [{"options": {}, "count": n} for n in (0, 6, True, 2.5, "2")]
            invalid.extend([{"options": {"duration": 45}, "count": 2},
                            {"options": {"quality": 480}, "count": 2},
                            {"options": {"voice": "piper:removed.onnx"}, "count": 2},
                            {"options": {}, "count": 2, "publish": True}])
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(WebError):
                    self.service.story_batch(payload)
                self.assertEqual(project_count(), 0)
        missing = dict(self.capabilities(), ok=False, renderer_ready=False,
                       message="Install the stories extra and set up FFmpeg in Settings.")
        with patch.object(self.service, "story_capabilities", return_value=missing):
            with self.assertRaises(WebError) as unavailable:
                self.service.story_batch({"options": {}, "count": 2})
            self.assertEqual(unavailable.exception.status, 409)
            self.assertIn("FFmpeg", str(unavailable.exception))
            self.assertEqual(project_count(), 0)
        with self.service.db() as db:
            # Persist real queued records without dispatching them so the test
            # deterministically exercises all-or-nothing batch admission.
            for i in range(19):
                db.conn.execute("INSERT INTO web_jobs VALUES(?,?)", (f"{i:032x}", json.dumps({"state": "queued"})))
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()):
            with self.assertRaises(WebError) as full:
                self.service.story_batch({"options": {"idea": "A window mystery."}, "count": 2})
            self.assertEqual(full.exception.status, 409)
            self.assertIn("queue", str(full.exception))
            self.assertEqual(project_count(), 0)

    def test_two_story_batch_produces_independent_projects_and_review_required_exports(self):
        first, second = plan(), plan()
        first["title"] = "The First Window"
        second["title"] = "The Second Window"
        second["scenes"][1]["narration"] = "The second window was a mirror into tomorrow."
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_service.write_story", side_effect=[(first, {"model": "local", "digest": "one"}),
                                                                        (second, {"model": "local", "digest": "two"})]) as writing, \
             patch("clipforge.story_audio.synthesize_story", side_effect=self.audio), \
             patch("clipforge.story_animation.render_story", side_effect=self.renderer), \
             patch("clipforge.worker.verify_artifact", return_value={}):
            batch = self.service.story_batch({"options": {"idea": "A strange window.", "duration": 30}, "count": 2})
            self.assertEqual(len(batch["projects"]), 2)
            self.assertEqual(len(batch["jobs"]), 2)
            self.assertEqual(len({p["id"] for p in batch["projects"]}), 2)
            for job in batch["jobs"]:
                self.assertEqual(self.wait_job(job["id"])["state"], "completed")
            self.assertEqual(writing.call_count, 2)
        projects = [self.service.get_story(p["id"])["project"] for p in batch["projects"]]
        self.assertEqual({p["title"] for p in projects}, {"The First Window", "The Second Window"})
        self.assertEqual(len({p["clip_id"] for p in projects}), 2)
        for project in projects:
            self.assertEqual(project["state"], "completed")
            self.assertEqual(project["options"]["voice"], "sapi:Test Voice")
            self.assertTrue(project["export_available"])
            self.assertEqual(len(project["exports"]), 1)
            clip = self.service.get_clip(project["clip_id"])
            self.assertEqual(clip["state"], "REVIEW_REQUIRED")
            self.assertFalse(clip["submitted"])
            self.assertTrue(self.service.media("clip", clip["id"])[0].is_file())
        self.assertEqual(len(self.service.library()["clips"]), 2)

    def test_actionable_narration_failure_is_shown_and_prior_export_is_preserved(self):
        project, _ = self.render_project()
        original = self.service.media("clip", project["clip_id"])[0]
        original_bytes = original.read_bytes()
        explanation = "The selected voice produced silent audio. Try another installed local voice."
        with patch.object(self.service, "story_capabilities", return_value=self.capabilities()), \
             patch("clipforge.story_audio.synthesize_story", side_effect=StoryAudioError(explanation)):
            job = self.service.story_action(project["id"], "generate", {})["job"]
            failed = self.wait_job(job["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["message"], explanation)
        saved = self.service.get_story(project["id"])["project"]
        self.assertEqual(saved["error"], explanation)
        self.assertFalse(saved["busy"])
        self.assertTrue(saved["export_available"])
        self.assertEqual(saved["clip_id"], project["clip_id"])
        self.assertEqual(saved["exports"], project["exports"])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(list(self.service._story_folder(project["id"]).glob("attempt-*")), [])
