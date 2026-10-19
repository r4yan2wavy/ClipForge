"""Studio transport, durable job and artifact safety regressions."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clipforge.common import Settings, atomic_json, iso_now, sha256_file, stable_id
from clipforge.web import ClipForgeHTTPServer, parse_range
from clipforge.web_service import WebError, WebService


PROBE = {"streams": [{"codec_type": "video", "width": 640, "height": 360}, {"codec_type": "audio"}],
         "format": {"duration": "10"}}


class StudioTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        settings = Settings()
        settings.paths.root = str(self.root)
        atomic_json(self.config, settings.model_dump())
        self.health_patch = patch.object(WebService, "health", lambda self: {
            name: {"ok": True, "message": "test local prerequisite"} for name in ("ffmpeg", "ffprobe", "speech", "ollama", "youtube")})
        self.health_patch.start()
        self.service = WebService(self.config)

    def tearDown(self):
        self.service.close()
        self.health_patch.stop()
        self.temporary.cleanup()

    def source(self):
        path = self.root / "owned.mp4"
        path.write_bytes(b"0123456789")
        with patch("clipforge.web_service.run_command", return_value=SimpleNamespace(stdout=json.dumps(PROBE))):
            return self.service.register_source(path)

    def payload(self, source):
        return {"source_id": source["id"], "mode": "manual", "title": "My reviewed clip", "framing": "fit",
                "start_s": 1.0, "end_s": 3.0, "caption_text": "Entered by me",
                "rights_category": "original", "permission_reference": "My own original recording",
                "creator_credit": "Recorded by me", "contribution": "Selected and explained this moment"}

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = next(j for j in self.service.library()["jobs"] if j["id"] == job_id)
            if job["state"] not in {"queued", "running"}:
                return job
            time.sleep(.02)
        self.fail("background task did not finish")

    @staticmethod
    def renderer(settings, media, folder, **kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / "clip.mp4"
        output.write_bytes(b"rendered video fixture")
        kwargs["on_stage"]("rendering")
        return {"video_path": str(output), "render_sha256": sha256_file(output), "caption_path": None,
                "render_spec": {"render": settings.render.model_dump()}, "width": settings.render.width,
                "height": settings.render.height, "duration_s": kwargs["end_s"] - kwargs["start_s"]}

    def render_fixture(self):
        source = self.source()
        with patch("clipforge.web_render.render_manual", side_effect=self.renderer), patch("clipforge.worker.verify_artifact", return_value={}):
            job = self.service.enqueue_clip(self.payload(source))["job"]
            result = self.wait_job(job["id"])
        self.assertEqual(result["state"], "completed", result)
        return self.service.library()["clips"][0]

    def test_manual_job_metadata_review_and_persistence(self):
        clip = self.render_fixture()
        self.assertEqual(clip["state"], "REVIEW_REQUIRED")
        self.assertNotIn("video_path", clip)
        self.assertNotIn("path", self.service.library()["sources"][0])
        with self.assertRaises(WebError):
            self.service.approve(clip["id"], {"reviewed": False})
        self.assertEqual(self.service.approve(clip["id"], {"reviewed": True})["state"], "PREPARED")
        edited = self.service.edit_clip(clip["id"], {"title": "Updated title", "description": "Updated description", "tags": ["original"]})
        self.assertEqual(edited["state"], "REVIEW_REQUIRED")
        self.service.close()
        self.service = WebService(self.config)
        restored = self.service.library()
        self.assertEqual(len(restored["sources"]), 1)
        self.assertEqual(restored["clips"][0]["title"], "Updated title")
        self.assertEqual(restored["jobs"][0]["state"], "completed")

    def test_submitted_snapshot_cannot_be_edited_or_approved(self):
        clip = self.render_fixture()
        with self.service.db() as db:
            folder, artifact = self.service._artifact(db, clip["id"])
            snapshot = (folder / "artifact.json").read_bytes()
            db.conn.execute("""INSERT INTO publications(clip_id,channel_id,source_sha256,render_sha256,
                artifact_json,metadata_json,video_path,total_bytes,state,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (clip["id"], "channel", artifact["source_sha256"],
                    artifact["render_sha256"], json.dumps(artifact), "{}", artifact["video_path"], 22, "needs_reconciliation", iso_now()))
        for mutation in (lambda: self.service.approve(clip["id"], {"reviewed": True}),
                         lambda: self.service.edit_clip(clip["id"], {"title": "Changed", "description": "Changed", "tags": []})):
            with self.assertRaises(WebError) as caught:
                mutation()
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual((folder / "artifact.json").read_bytes(), snapshot)
        public = self.service.get_clip(clip["id"])
        self.assertTrue(public["submitted"])
        self.assertNotIn("artifact_json", public["publication"])
        self.assertNotIn("session_uri", public["publication"])

    def test_restart_marks_pending_jobs_interrupted_and_blocks_second_studio(self):
        with self.assertRaises(WebError):
            WebService(self.config)
        with self.service.db() as db:
            self.service._save_job(db, {"id": "interrupted-job", "state": "running", "stage": "rendering", "kind": "clip", "payload": {}})
        self.service.close()
        self.service = WebService(self.config)
        self.assertEqual(self.service.library()["jobs"][0]["state"], "interrupted")

    def test_invalid_bounds_nonfinite_and_caption_limit_rejected(self):
        source = self.source()
        for changes in ({"start_s": float("nan")}, {"end_s": 12.0}, {"start_s": True}, {"caption_text": "x" * 501}):
            payload = dict(self.payload(source), **changes)
            with self.assertRaises(WebError):
                self.service.enqueue_clip(payload)
        self.assertEqual(self.service.library()["jobs"], [])

    def test_settings_allowlist_atomic_preserve_and_rollback(self):
        before = json.loads(self.config.read_text())
        self.service.update_settings({"render": {"font_size": 70}})
        after = json.loads(self.config.read_text())
        self.assertEqual(after["render"]["font_size"], 70)
        self.assertEqual(after["discovery"], before["discovery"])
        with self.assertRaises(WebError):
            self.service.update_settings({"paths": {"state": "elsewhere.sqlite"}})
        with self.assertRaises(WebError):
            self.service.update_settings({"render": {"font_size": -1}})
        self.assertEqual(json.loads(self.config.read_text()), after)

    def test_publication_confirmation_preserves_reviewed_snapshot(self):
        clip = self.render_fixture()
        self.service.approve(clip["id"], {"reviewed": True})
        self.service.settings.upload.enabled = True
        self.service.settings.upload.target_channel_id = "my-channel"
        declarations = {"confirmed": True, "made_for_kids": False, "contains_synthetic_media": False, "schedule": None}
        with patch.object(self.service, "_enqueue", side_effect=lambda db, kind, payload, **kw: payload):
            queued_payload = self.service.publish(clip["id"], declarations)
        self.assertIn("reviewed_artifact_hash", queued_payload)
        self.service.edit_clip(clip["id"], {"title": "Changed after confirmation", "description": "Changed", "tags": []})
        self.service.approve(clip["id"], {"reviewed": True})
        with self.service.db() as db, patch("clipforge.upload.upload_prepared") as upload:
            with self.assertRaises(WebError) as caught:
                self.service._youtube_task(db, {"id": "test-publish", "kind": "publish", "payload": queued_payload})
            self.assertIn("changed after", str(caught.exception))
            upload.assert_not_called()

    def test_synthetic_sample_publication_rejected_server_side(self):
        clip = self.render_fixture()
        with self.service.db() as db:
            folder, artifact = self.service._artifact(db, clip["id"])
            artifact["web_is_demo"] = True
            atomic_json(folder / "artifact.json", artifact)
        self.service.approve(clip["id"], {"reviewed": True})
        self.service.settings.upload.enabled = True
        self.service.settings.upload.target_channel_id = "my-channel"
        with self.assertRaises(WebError) as caught:
            self.service.publish(clip["id"], {"confirmed": True, "made_for_kids": False, "contains_synthetic_media": False, "schedule": None})
        self.assertIn("sample", str(caught.exception))

    def test_explicit_ai_retry_reaches_engine_after_previous_input_failure(self):
        source = self.source()
        job = {"id": "ai-retry", "payload": dict(self.payload(source), mode="ai"),
               "source_id": source["id"], "retry_requested": True}
        with self.service.db() as db, patch("clipforge.worker.prepare_manifest", return_value={"state": "NO_CANDIDATES"}) as prepare:
            self.service._prepare(db, job)
        self.assertTrue(prepare.call_args.kwargs["retry"])
        self.assertIn("No suitable", job["result_message"])

    def saved_publication(self):
        clip = self.render_fixture()
        self.service.approve(clip["id"], {"reviewed": True})
        self.service.settings.upload.enabled = True
        self.service.settings.upload.target_channel_id = "saved-channel"
        with self.service.db() as db:
            folder, artifact = self.service._artifact(db, clip["id"])
            artifact["source_manifest"].update(made_for_kids=True, contains_synthetic_media=False, schedule=None)
            atomic_json(folder / "artifact.json", artifact)
            db.conn.execute("""INSERT INTO publications(clip_id,channel_id,source_sha256,render_sha256,
                artifact_json,metadata_json,video_path,total_bytes,state,session_uri,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (clip["id"], "saved-channel", artifact["source_sha256"],
                    artifact["render_sha256"], json.dumps(artifact, sort_keys=True, ensure_ascii=False),
                    json.dumps({"status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": True,
                        "containsSyntheticMedia": False}}), artifact["video_path"], 22, "uploading",
                    "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=fixture", iso_now()))
        return clip, folder

    def test_resume_uses_saved_intent_without_rewriting_declarations(self):
        clip, folder = self.saved_publication()
        snapshot = (folder / "artifact.json").read_bytes()
        record = self.service.get_clip(clip["id"])
        self.assertTrue(record["can_resume"])
        with patch.object(self.service, "_enqueue", side_effect=lambda db, kind, payload, **kw: payload):
            payload = self.service.resume(clip["id"], {"confirmed": True})
        self.assertNotIn("made_for_kids", payload)
        self.assertNotIn("session_uri", payload)
        with self.service.db() as db, patch("clipforge.upload.upload_prepared", return_value={"state": "upload_accepted"}) as upload, patch("clipforge.upload._body") as new_body:
            self.service._youtube_task(db, {"id": "resume-fixture", "kind": "resume", "payload": payload})
            upload.assert_called_once_with(self.service.settings, db, folder, True)
            new_body.assert_not_called()
        self.assertEqual((folder / "artifact.json").read_bytes(), snapshot)

    def test_resume_rejects_wrong_channel_terminal_and_lost_intent(self):
        clip, folder = self.saved_publication()
        with patch.object(self.service, "_enqueue", side_effect=lambda db, kind, payload, **kw: payload):
            payload = self.service.resume(clip["id"], {"confirmed": True})
        self.service.settings.upload.target_channel_id = "different-channel"
        self.assertFalse(self.service.get_clip(clip["id"])["can_resume"])
        with self.assertRaises(WebError):
            self.service.resume(clip["id"], {"confirmed": True})
        with self.service.db() as db, patch("clipforge.upload.upload_prepared") as upload:
            with self.assertRaises(WebError):
                self.service._youtube_task(db, {"id": "wrong-channel-resume", "kind": "resume", "payload": payload})
            upload.assert_not_called()
        self.service.settings.upload.target_channel_id = "saved-channel"
        with self.service.db() as db:
            db.conn.execute("UPDATE publications SET state='needs_reconciliation' WHERE clip_id=?", (clip["id"],))
        with self.assertRaises(WebError):
            self.service.resume(clip["id"], {"confirmed": True})
        with self.service.db() as db, patch("clipforge.upload.upload_prepared") as upload:
            db.conn.execute("DELETE FROM publications WHERE clip_id=?", (clip["id"],))
            with self.assertRaises(WebError):
                self.service._youtube_task(db, {"id": "lost-intent-resume", "kind": "resume", "payload": payload})
            upload.assert_not_called()

    def test_http_origin_token_content_limits_ranges_and_upload_cleanup(self):
        source = self.source()
        server = ClipForgeHTTPServer(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        def request(method, path, body=None, **headers):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            result = response.status, dict(response.getheaders()), data
            connection.close()
            return result

        try:
            status, headers, body = request("GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            token = json.loads(body)["token"]
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertEqual(request("GET", "/api/bootstrap", Host="attacker.example")[0], 403)
            self.assertEqual(request("GET", "/api/bootstrap", Origin="https://attacker.example")[0], 403)
            self.assertEqual(request("POST", "/api/demo", "{}", **{"Content-Type": "application/json"})[0], 403)
            auth = {"X-ClipForge-Token": token, "Content-Type": "application/json", "Origin": f"http://localhost:{port}"}
            status, _, body = request("POST", "/api/links", json.dumps({"url": "https://www.youtube.com/watch?v=abcdefghijk", "rights_confirmed": True}), **auth)
            self.assertEqual(status, 202)
            link = json.loads(body)["link"]
            self.assertEqual(link["state"], "needs_file")
            status, _, body = request("POST", f"/api/links/{link['id']}/attach", json.dumps({"source_id": source["id"]}), **auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["link"]["state"], "ready")
            self.assertEqual(request("GET", "/guide/links")[0], 200)
            self.assertEqual(request("GET", "/guide/social")[0], 200)
            self.assertEqual(request("GET", "/api/social/instagram/creator")[0], 409)
            self.assertEqual(request("POST", "/api/clips/missing/social/tiktok/publish", "{}", **auth)[0], 400)
            self.assertEqual(request("POST", "/api/demo", "{}", **dict(auth, Origin="null"))[0], 403)
            self.assertEqual(request("POST", "/api/settings", '{"brain":{"max_clips":NaN}}', **auth)[0], 400)
            self.assertEqual(request("POST", "/api/settings", "{}", **dict(auth, **{"Content-Length": "70000"}))[0], 413)
            status, headers, body = request("GET", source["url"], Range="bytes=2-5")
            self.assertEqual((status, body), (206, b"2345"))
            self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
            self.assertEqual(request("GET", source["url"], Range="bytes=20-")[0], 416)
            self.assertEqual(request("GET", "/media/source/notregistered")[0], 404)
            self.assertEqual(request("GET", "/%2e%2e/common.py")[0], 404)
            upload_headers = {"X-ClipForge-Token": token, "Content-Type": "application/octet-stream"}
            with patch("clipforge.web_service.run_command", side_effect=ValueError("unreadable media")):
                self.assertEqual(request("POST", "/api/sources?name=broken.mp4", b"bad", **upload_headers)[0], 400)
            self.assertEqual(list((self.service.data_dir / "sources").iterdir()), [])
            with patch("clipforge.web_service.run_command", return_value=SimpleNamespace(stdout=json.dumps(PROBE))):
                status, _, body = request("POST", "/api/sources?name=owned.mp4", b"0123456789", **upload_headers)
            self.assertEqual(status, 201)
            self.assertEqual(json.loads(body)["bytes"], 10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_platform_links_are_references_and_attached_file_keeps_provenance(self):
        for url, platform in (("https://www.youtube.com/watch?v=abcdefghijk", "youtube"),
                              ("https://kick.com/my-channel/videos/recording", "kick"),
                              ("https://www.instagram.com/reel/ABC123/", "instagram"),
                              ("https://www.tiktok.com/@creator/video/123456", "tiktok")):
            result = self.service.import_link({"url": url, "rights_confirmed": True})
            self.assertEqual(result["link"]["platform"], platform)
            self.assertEqual(result["link"]["state"], "needs_file")
            self.assertNotIn("job", result)
            self.assertNotIn("private_url", result["link"])
        source = self.source()
        link = self.service.library()["links"][0]
        attached = self.service.attach_link(link["id"], {"source_id": source["id"]})
        self.assertEqual(attached["link"]["state"], "ready")
        self.assertEqual(attached["source"]["source_url"], link["url"])
        self.assertEqual(attached["source"]["id"], source["id"])
        with self.assertRaises(WebError):
            self.service.import_link({"url": "https://example.com/a.mp4", "rights_confirmed": False})

    def test_direct_link_import_downloads_then_registers_and_hides_signed_query(self):
        def download(url, target, **kwargs):
            self.assertIn("signature=private-signed-query", url)
            target.write_bytes(b"0123456789")
            return target
        with patch("clipforge.link_import.download_video", side_effect=download), patch("clipforge.web_service.run_command", return_value=SimpleNamespace(stdout=json.dumps(PROBE))):
            result = self.service.import_link({"url": "https://media.example.com/owned.mp4?signature=private-signed-query", "rights_confirmed": True})
            job = self.wait_job(result["job"]["id"])
        self.assertEqual(job["state"], "completed", job)
        library = self.service.library()
        self.assertEqual(library["links"][0]["state"], "ready")
        self.assertEqual(len(library["sources"]), 1)
        self.assertNotIn("private-signed-query", json.dumps(library))

    def test_failed_link_import_cleans_up_and_records_explicit_retry(self):
        from clipforge.link_import import LinkError
        def download(url, target, **kwargs):
            target.write_bytes(b"incomplete")
            raise LinkError("The transfer was interrupted.")
        with patch("clipforge.link_import.download_video", side_effect=download):
            result = self.service.import_link({"url": "https://media.example.com/owned.mp4", "rights_confirmed": True})
            job = self.wait_job(result["job"]["id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.service.library()["links"][0]["state"], "failed")
        self.assertEqual(list((self.service.data_dir / "sources").iterdir()), [])

    def test_social_queue_preserves_review_and_blocks_duplicate_requests(self):
        from clipforge.social_publish import schema
        clip = self.render_fixture()
        self.service.approve(clip["id"], {"reviewed": True})
        self.service.settings.instagram.enabled = True
        self.service.settings.instagram.target_account_id = "123"
        atomic_json(self.root / "instagram.json", {"access_token": "test-only", "account_id": "123", "login_type": "facebook_business"})
        self.service.settings.instagram.credentials_file = str(self.root / "instagram.json")
        options = {"confirmed": True, "caption": "A reviewed caption", "share_to_feed": False, "account_id": "123"}
        # Keep queued work deterministic while verifying the server-side duplicate guard.
        with patch.object(self.service.pending, "put"):
            result = self.service.social_action(clip["id"], "instagram", "publish", options)
            self.assertEqual(result["job"]["platform"], "instagram")
            self.assertEqual(result["job"]["target_clip_id"], clip["id"])
            with self.assertRaises(WebError):
                self.service.social_action(clip["id"], "instagram", "publish", options)
        with self.service.db() as db:
            job = self.service._job(db, result["job"]["id"])
            self.service.edit_clip(clip["id"], {"title": "Edited after consent", "description": "Changed", "tags": []})
            self.service.approve(clip["id"], {"reviewed": True})
            with patch("clipforge.social_publish.publish") as publish:
                with self.assertRaises(WebError):
                    self.service._social_task(db, job)
                publish.assert_not_called()

    def test_social_intent_freezes_metadata_but_allows_youtube_cross_post_without_artifact_changes(self):
        clip = self.render_fixture()
        self.service.approve(clip["id"], {"reviewed": True})
        with self.service.db() as db:
            folder, artifact = self.service._artifact(db, clip["id"])
            before = (folder / "artifact.json").read_bytes()
            db.conn.execute("""INSERT INTO social_publications(clip_id,platform,target_account_id,artifact_hash,
                artifact_json,options_json,video_path,video_sha256,total_bytes,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (clip["id"], "instagram", "123", stable_id(artifact),
                    json.dumps(artifact), "{}", artifact["video_path"], artifact["render_sha256"],
                    Path(artifact["video_path"]).stat().st_size, "processing", iso_now(), iso_now()))
        self.assertTrue(self.service.get_clip(clip["id"])["submitted"])
        with self.assertRaises(WebError):
            self.service.edit_clip(clip["id"], {"title": "Changed", "description": "Changed", "tags": []})
        self.service.settings.upload.enabled = True
        self.service.settings.upload.target_channel_id = "youtube-channel"
        with patch.object(self.service, "_enqueue", side_effect=lambda db, kind, payload, **kw: payload):
            payload = self.service.publish(clip["id"], {"confirmed": True, "made_for_kids": False, "contains_synthetic_media": False, "schedule": None})
        with self.service.db() as db, patch("clipforge.upload.upload_prepared", return_value={"state": "upload_accepted"}) as upload:
            self.service._youtube_task(db, {"id": "cross-post", "kind": "publish", "payload": payload})
            self.assertEqual(upload.call_args.kwargs["publication_options"]["made_for_kids"], False)
        self.assertEqual((folder / "artifact.json").read_bytes(), before)

    def test_range_parser(self):
        self.assertEqual(parse_range("bytes=-4", 10), (6, 9))
        self.assertEqual(parse_range("bytes=3-", 10), (3, 9))
        self.assertEqual(parse_range("bytes=0-999", 10), (0, 9))
        for value in ("bytes=-0", "bytes=10-11", "bytes=5-3", "bytes=0-1,3-5", "bytes=-", "items=1-2"):
            with self.assertRaises(WebError):
                parse_range(value, 10)


if __name__ == "__main__":
    unittest.main()
