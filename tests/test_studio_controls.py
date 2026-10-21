"""Operator deletion, cancellation, timing visibility and upload queue safety."""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_web
from clipforge.common import atomic_json, iso_now
from clipforge.job_control import checkpoint, report
from clipforge.web_service import WebError, WebService


class StudioControlsTest(unittest.TestCase):
    setUp = test_web.StudioTest.setUp
    tearDown = test_web.StudioTest.tearDown
    source = test_web.StudioTest.source
    payload = test_web.StudioTest.payload
    wait_job = test_web.StudioTest.wait_job
    renderer = staticmethod(test_web.StudioTest.renderer)
    render_fixture = test_web.StudioTest.render_fixture
    saved_publication = test_web.StudioTest.saved_publication

    def test_delete_clip_removes_disk_files_but_keeps_source_and_restart_history(self):
        clip = self.render_fixture()
        with self.service.db() as db:
            folder, _ = self.service._artifact(db, clip["id"])
        (folder / "captions.srt").write_text("local captions", encoding="utf-8")
        original = self.root / "owned.mp4"
        result = self.service.delete_clip(clip["id"], {"confirmed": True})
        self.assertGreater(result["bytes_freed"], 0)
        self.assertFalse(folder.exists())
        self.assertTrue(original.exists())
        library = self.service.library()
        self.assertEqual(library["clips"], [])
        self.assertNotIn(clip["id"], library["jobs"][0]["clip_ids"])
        with self.service.db() as db:
            self.assertEqual(db.conn.execute("SELECT active FROM artifacts WHERE clip_id=?", (clip["id"],)).fetchone()[0], 0)
            self.assertIsNotNone(db.conn.execute("SELECT 1 FROM web_deleted_clips WHERE clip_id=?", (clip["id"],)).fetchone())
        self.service.close()
        self.service = WebService(self.config)
        self.assertEqual(self.service.library()["clips"], [])
        with self.assertRaises(WebError):
            self.service.media("clip", clip["id"])

    def test_delete_clip_validates_consent_and_keeps_files_on_cleanup_error(self):
        clip = self.render_fixture()
        for payload in ({}, {"confirmed": False}, {"confirmed": True, "path": str(self.root)}):
            with self.assertRaises(WebError):
                self.service.delete_clip(clip["id"], payload)
        with patch("clipforge.web_service.shutil.rmtree", side_effect=PermissionError):
            with self.assertRaises(WebError) as caught:
                self.service.delete_clip(clip["id"], {"confirmed": True})
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.service.get_clip(clip["id"])["id"], clip["id"])
        with self.service.db() as db:
            self.assertIsNone(db.conn.execute("SELECT 1 FROM web_deleted_clips").fetchone())

    def test_delete_clip_refuses_outside_and_shared_source_folders(self):
        clip = self.render_fixture()
        with self.service.db() as db:
            folder, _ = self.service._artifact(db, clip["id"])
            source = self.service._sources(db)[0]
            source["path"] = str(folder / "clip.mp4")
            db.conn.execute("UPDATE web_sources SET data=? WHERE id=?", (json.dumps(source), source["id"]))
        with self.assertRaises(WebError):
            self.service.delete_clip(clip["id"], {"confirmed": True})
        self.assertTrue(folder.is_dir())
        outside = self.root / "external" / clip["id"]
        outside.mkdir(parents=True)
        atomic_json(outside / "artifact.json", {"clip_id": clip["id"]})
        with self.service.db() as db:
            db.conn.execute("UPDATE artifacts SET artifact_dir=? WHERE clip_id=?", (str(outside), clip["id"]))
        with self.assertRaises(WebError):
            self.service.delete_clip(clip["id"], {"confirmed": True})
        self.assertTrue(outside.is_dir())

    def test_partial_delete_failure_preserves_entry_for_cleanup_retry(self):
        clip = self.render_fixture()
        with self.service.db() as db:
            folder, _ = self.service._artifact(db, clip["id"])
        def partial_failure(path):
            (path / "artifact.json").unlink()
            raise PermissionError("open video handle")
        with patch("clipforge.web_service.shutil.rmtree", side_effect=partial_failure):
            with self.assertRaises(WebError):
                self.service.delete_clip(clip["id"], {"confirmed": True})
        self.assertEqual(self.service.get_clip(clip["id"])["id"], clip["id"])
        self.service.delete_clip(clip["id"], {"confirmed": True})
        self.assertFalse(folder.exists())

    def test_deleting_published_clip_keeps_remote_record_but_unfinished_upload_blocks(self):
        clip, folder = self.saved_publication()
        self.assertFalse(self.service.get_clip(clip["id"])["can_delete"])
        with self.assertRaises(WebError):
            self.service.delete_clip(clip["id"], {"confirmed": True})
        self.assertTrue(folder.exists())
        with self.service.db() as db:
            db.conn.execute("UPDATE publications SET video_id='uploaded-fixture',state='upload_accepted' WHERE clip_id=?", (clip["id"],))
        self.assertTrue(self.service.get_clip(clip["id"])["can_delete"])
        self.service.delete_clip(clip["id"], {"confirmed": True})
        with self.service.db() as db:
            record = db.conn.execute("SELECT video_id FROM publications WHERE clip_id=?", (clip["id"],)).fetchone()
            self.assertEqual(record[0], "uploaded-fixture")

    def test_cancel_running_and_queued_jobs_then_delete_queue_entry_without_killing_worker(self):
        source = self.source()
        entered = threading.Event()
        def preparing(db, job):
            entered.set()
            while True:
                checkpoint()
                time.sleep(.01)
        with patch.object(self.service, "_prepare", side_effect=preparing):
            first = self.service.enqueue_clip(self.payload(source))["job"]
            self.assertTrue(entered.wait(2))
            second = self.service.enqueue_clip(self.payload(source))["job"]
            with self.assertRaises(WebError):
                self.service.cancel_job(first["id"], {})
            cancelled = self.service.cancel_job(second["id"], {"confirmed": True})["job"]
            self.assertEqual(cancelled["state"], "cancelled")
            self.service.delete_job(second["id"], {"confirmed": True})
            self.service.cancel_job(first["id"], {"confirmed": True})
            self.assertEqual(self.wait_job(first["id"])["state"], "cancelled")
        self.assertTrue(self.service.thread.is_alive())
        self.assertTrue((self.root / "owned.mp4").exists())
        with patch("clipforge.web_render.render_manual", side_effect=self.renderer), patch("clipforge.worker.verify_artifact", return_value={}):
            retried = self.service.retry(first["id"])["job"]
            self.assertEqual(self.wait_job(retried["id"])["state"], "completed")

    def test_cancel_link_cleans_partial_source_and_remains_explicitly_retryable(self):
        entered = threading.Event()
        def download(url, target, **kwargs):
            target.write_bytes(b"partial")
            entered.set()
            while not kwargs["cancelled"]():
                time.sleep(.01)
            from clipforge.link_import import LinkError
            raise LinkError("Stopped")
        with patch("clipforge.link_import.download_video", side_effect=download):
            result = self.service.import_link({"url": "https://media.example.com/owned.mp4", "rights_confirmed": True})
            self.assertTrue(entered.wait(2))
            self.service.cancel_job(result["job"]["id"], {"confirmed": True})
            stopped = self.wait_job(result["job"]["id"])
        self.assertEqual(stopped["state"], "cancelled")
        self.assertTrue(stopped["can_delete"])
        self.assertEqual(list((self.service.data_dir / "sources").iterdir()), [])
        self.assertEqual(self.service.library()["links"][0]["state"], "failed")
        self.assertFalse(stopped["cancel_requested"])

    def test_cancel_after_manual_render_cleans_unregistered_output(self):
        source = self.source()
        rendered = threading.Event()
        def renderer(settings, media, folder, **kwargs):
            result = self.renderer(settings, media, folder, **kwargs)
            rendered.set()
            while True:
                checkpoint()
                time.sleep(.01)
            return result
        with patch("clipforge.web_render.render_manual", side_effect=renderer):
            job = self.service.enqueue_clip(self.payload(source))["job"]
            self.assertTrue(rendered.wait(2))
            self.service.cancel_job(job["id"], {"confirmed": True})
            self.assertEqual(self.wait_job(job["id"])["state"], "cancelled")
        self.assertFalse((self.service.settings.resolve(self.service.settings.paths.prepared) / ("manual-" + job["id"])).exists())
        self.assertTrue((self.root / "owned.mp4").exists())

    def test_public_job_timing_hides_private_profile_and_cancel_upload_is_rejected(self):
        with self.service.db() as db:
            job = {"id": "timing-fixture", "kind": "clip", "state": "running", "stage": "rendering", "clip_ids": [],
                   "payload": {}, "mode": "manual", "source_id": None, "timing_profile": "private-model-path"}
            from clipforge.progress import start_stage, update_progress
            now = time.time()
            start_stage(job, "rendering", now=now - 4)
            for age, amount in ((4, 0), (2, 20), (0, 40)):
                update_progress(job, amount, 100, now=now - age)
            self.service._save_job(db, job)
            record = self.service.job_record(job, db)
            self.assertEqual(record["stage_percent"], 40)
            self.assertGreater(record["eta_seconds"], 0)
            self.assertNotIn("private-model-path", json.dumps(record))
            self.assertNotIn("_timing", record)
            job.update(kind="publish", state="queued")
            self.service._save_job(db, job)
        with self.assertRaises(WebError):
            self.service.cancel_job(job["id"], {"confirmed": True})

    def test_duplicate_queued_youtube_upload_is_rejected(self):
        clip = self.render_fixture()
        self.service.approve(clip["id"], {"reviewed": True})
        self.service.settings.upload.enabled = True
        self.service.settings.upload.target_channel_id = "fixture-channel"
        with self.service.db() as db:
            queued = {"id": "queued-publish-fixture", "kind": "publish", "state": "queued", "payload": {"clip_id": clip["id"]}}
            self.service._save_job(db, queued)
        with self.assertRaises(WebError) as caught:
            self.service.publish(clip["id"], {"confirmed": True, "made_for_kids": False, "contains_synthetic_media": False})
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
