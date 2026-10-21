"""Voice queue contracts, preview media isolation, and local lifecycle safety."""
from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest.mock import patch

import test_web
from clipforge.job_control import checkpoint
from clipforge.story_audio import StoryAudioError
from clipforge.web import ClipForgeHTTPServer
from clipforge.web_service import WebError


class StoryVoiceServiceTest(unittest.TestCase):
    setUp = test_web.StudioTest.setUp
    tearDown = test_web.StudioTest.tearDown
    wait_job = test_web.StudioTest.wait_job

    @staticmethod
    def library():
        return {"voices": [{"id": "sapi:Fixture", "name": "Fixture"}],
                "catalog": [{"id": "en_US-fixture-medium", "name": "Fixture neural",
                             "installed": False, "voice_id": "piper:en_US-fixture-medium.onnx"}],
                "engine_ready": True, "engine_message": "Local voices engine is ready."}

    def install(self):
        return self.service.story_voice_action("install", {"id": "en_US-fixture-medium"})["job"]

    def preview(self):
        return self.service.story_voice_action("preview", {"voice": "sapi:Fixture"})["job"]

    @staticmethod
    def preview_audio(voice_id, settings, folder):
        path = folder / "sample.wav"
        path.write_bytes(b"RIFF-local-preview-fixture")
        return path

    def test_install_rejects_unknown_pack_extra_fields_and_missing_runtime_before_queuing(self):
        with patch.object(self.service, "story_voices", return_value=self.library()):
            for payload in ({"id": "../../outside"}, {"id": "https://example.test/voice"},
                            {"id": "en_US-fixture-medium", "url": "https://example.test/model"},
                            {"id": 1}, {"id": "x" * 241}, {}):
                with self.subTest(payload=payload), self.assertRaises(WebError):
                    self.service.story_voice_action("install", payload)
        unavailable = dict(self.library(), engine_ready=False, engine_message="Install local voices support first.")
        with patch.object(self.service, "story_voices", return_value=unavailable), self.assertRaises(WebError) as error:
            self.install()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.service.library()["jobs"], [])

    def test_install_job_is_cancellable_idempotent_while_active_and_publicly_identifiable(self):
        entered, release = threading.Event(), threading.Event()

        def install(identifier, settings):
            entered.set()
            while not release.wait(.01):
                checkpoint()

        with patch.object(self.service, "story_voices", return_value=self.library()), \
             patch("clipforge.story_voices.install_voice", side_effect=install):
            job = self.install()
            self.assertTrue(entered.wait(2))
            duplicate = self.install()
            self.assertEqual(job["id"], duplicate["id"])
            self.assertTrue(job["can_cancel"])
            self.assertEqual(job["kind"], "story_voice_install")
            self.assertEqual(job["voice_catalog_id"], "en_US-fixture-medium")
            self.assertNotIn("payload", job)
            self.service.cancel_job(job["id"], {"confirmed": True})
            result = self.wait_job(job["id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(self.service.stories()["projects"], [])

    def test_preview_export_uses_job_scoped_media_and_delete_removes_preview_file(self):
        with patch("clipforge.story_audio.discover_voices", return_value=self.library()["voices"]), \
             patch("clipforge.story_voices.preview_voice", side_effect=self.preview_audio):
            result = self.wait_job(self.preview()["id"])
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["voice_id"], "sapi:Fixture")
        self.assertEqual(result["voice_preview_url"], "/media/story-voice/" + result["id"])
        path, filename = self.service.story_voice_media(result["id"])
        self.assertEqual(filename, "voice-preview.wav")
        self.assertTrue(path.is_file())
        self.assertNotIn(str(self.root), json.dumps(result))
        self.service.delete_job(result["id"], {"confirmed": True})
        self.assertFalse(path.exists())
        with self.assertRaises(WebError):
            self.service.story_voice_media(result["id"])

    def test_preview_rejects_uninstalled_voices_and_cannot_expose_outside_files(self):
        with patch("clipforge.story_audio.discover_voices", return_value=[]), self.assertRaises(WebError):
            self.preview()
        outside = self.root / "outside.wav"
        outside.write_bytes(b"keep private")
        with patch("clipforge.story_audio.discover_voices", return_value=self.library()["voices"]), \
             patch("clipforge.story_voices.preview_voice", return_value=outside):
            result = self.wait_job(self.preview()["id"])
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("voice_preview_url", result)
        self.assertEqual(outside.read_bytes(), b"keep private")
        with self.assertRaises(WebError):
            self.service.story_voice_media(result["id"])

    def test_voice_install_can_retry_explicitly_and_does_not_create_story_project(self):
        with patch.object(self.service, "story_voices", return_value=self.library()), \
             patch("clipforge.story_voices.install_voice", side_effect=StoryAudioError("Voice download interrupted.")):
            failed = self.wait_job(self.install()["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["message"], "Voice download interrupted.")
        with patch.object(self.service, "story_voices", return_value=self.library()), \
             patch("clipforge.story_voices.install_voice", return_value={}):
            retried = self.service.retry(failed["id"])["job"]
            result = self.wait_job(retried["id"])
        self.assertNotEqual(retried["id"], failed["id"])
        self.assertEqual(result["state"], "completed")
        with self.service.db() as db:
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM web_stories").fetchone()[0], 0)

    def test_http_voice_routes_use_existing_origin_token_and_media_range_controls(self):
        server = ClipForgeHTTPServer(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        def request(method, path, payload=None, token=True, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            data = json.dumps(payload) if payload is not None else None
            selected = {"Host": f"127.0.0.1:{port}", **(headers or {})}
            if data is not None:
                selected["Content-Type"] = "application/json"
                if token:
                    selected["X-ClipForge-Token"] = self.service.token
            conn.request(method, path, body=data, headers=selected)
            response = conn.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            conn.close()
            return result

        try:
            with patch.object(self.service, "story_voices", return_value=self.library()), \
                 patch("clipforge.story_audio.discover_voices", return_value=self.library()["voices"]), \
                 patch("clipforge.story_voices.preview_voice", side_effect=self.preview_audio):
                self.assertEqual(request("GET", "/api/story-voices")[0], 200)
                self.assertEqual(request("POST", "/api/story-voices/preview", {"voice": "sapi:Fixture"}, token=False)[0], 403)
                status, _, data = request("POST", "/api/story-voices/preview", {"voice": "sapi:Fixture"})
                self.assertEqual(status, 202)
                result = self.wait_job(json.loads(data)["job"]["id"])
            status, headers, data = request("GET", result["voice_preview_url"], headers={"Range": "bytes=0-3"})
            self.assertEqual(status, 206)
            self.assertEqual(data, b"RIFF")
            self.assertIn(headers["Content-Type"], {"audio/wav", "audio/x-wav"})
            self.assertEqual(request("GET", "/media/story-voice/not-a-real-job")[0], 404)
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
