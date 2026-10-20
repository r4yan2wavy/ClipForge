"""Actual HTTP import → FFmpeg render → review → download, without model mocks."""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from clipforge.common import Settings, atomic_json, run_command
from clipforge.web import create_server


class LocalStudioFlowTests(unittest.TestCase):
    def test_uploaded_video_renders_reviews_downloads_and_survives_restart(self):
        ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG") or shutil.which("ffmpeg")
        ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE") or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg + ffprobe are required for a real local-app flow")
        with tempfile.TemporaryDirectory(prefix="clipforge-http-flow-") as temporary:
            root = Path(temporary)
            source = root / "original recording.mp4"
            run_command([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=10:duration=4", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=4", "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)])
            config = root / "config.json"
            settings = Settings()
            settings.render.ffmpeg, settings.render.ffprobe = ffmpeg, ffprobe
            atomic_json(config, settings.model_dump())
            server = create_server(config, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            token = None

            def request(method, path, value=None, raw=None, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
                sent = dict(headers or {})
                if token:
                    sent["X-ClipForge-Token"] = token
                body = raw
                if value is not None:
                    body = json.dumps(value).encode()
                    sent["Content-Type"] = "application/json"
                connection.request(method, path, body=body, headers=sent)
                response = connection.getresponse()
                received, received_headers, status = response.read(), dict(response.getheaders()), response.status
                connection.close()
                return status, received_headers, received

            try:
                status, _, body = request("GET", "/api/bootstrap")
                self.assertEqual(status, 200)
                bootstrap = json.loads(body)
                token = bootstrap["token"]
                self.assertTrue(bootstrap["capabilities"]["ffmpeg"]["ok"])
                self.assertFalse(bootstrap["settings"]["upload"]["enabled"])
                status, _, body = request("POST", "/api/sources?name=" + quote(source.name),
                    raw=source.read_bytes(), headers={"Content-Type": "application/octet-stream"})
                self.assertEqual(status, 201, body)
                registered = json.loads(body)
                self.assertNotIn("path", registered)
                status, _, body = request("POST", "/api/jobs", {
                    "source_id": registered["id"], "mode": "manual", "title": "My actual local render",
                    "framing": "blur", "start_s": 0.4, "end_s": 2.4,
                    "caption_text": "Manually entered test overlay", "rights_category": "original",
                    "permission_reference": "Generated synthetic fixture", "creator_credit": "ClipForge test fixture",
                    "contribution": "A synthetic video used to verify rendering, without speech inference."})
                self.assertEqual(status, 202, body)
                job_id = json.loads(body)["job"]["id"]
                deadline = time.monotonic() + 45
                while True:
                    status, _, body = request("GET", "/api/library")
                    self.assertEqual(status, 200)
                    library = json.loads(body)
                    job = next(j for j in library["jobs"] if j["id"] == job_id)
                    if job["state"] not in {"queued", "running"}:
                        break
                    self.assertLess(time.monotonic(), deadline, "The real render must finish within the test limit")
                    time.sleep(.2)
                self.assertEqual(job["state"], "completed", job)
                self.assertEqual(len(library["clips"]), 1)
                clip = library["clips"][0]
                self.assertEqual((clip["width"], clip["height"]), (1080, 1920))
                self.assertEqual(clip["mode"], "manual")
                self.assertAlmostEqual(clip["duration_s"], 2.0)
                status, headers, video_bytes = request("GET", clip["download_url"])
                self.assertEqual(status, 200)
                self.assertIn("attachment", headers["Content-Disposition"])
                self.assertGreater(len(video_bytes), 1000)
                status, headers, chunk = request("GET", clip["video_url"], headers={"Range": "bytes=16-63"})
                self.assertEqual(status, 206)
                self.assertEqual(chunk, video_bytes[16:64])
                status, _, captions = request("GET", clip["caption_url"])
                self.assertEqual(status, 200)
                self.assertIn(b"Manually entered", captions)
                status, _, body = request("POST", "/api/clips/" + clip["id"], {
                    "title": "Reviewed local clip", "description": "Verified synthetic test content.", "tags": ["local"]})
                self.assertEqual(status, 200, body)
                status, _, body = request("POST", "/api/clips/" + clip["id"] + "/approve", {"reviewed": True})
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body)["state"], "PREPARED")
                status, _, _ = request("POST", "/api/clips/" + clip["id"] + "/publish", {
                    "confirmed": True, "made_for_kids": False, "contains_synthetic_media": False, "schedule": None})
                self.assertEqual(status, 409, "Uploads remain disabled without explicit configuration")
                # The rendered MP4 and captions are self-contained after original cleanup.
                source_id = library["sources"][0]["id"]
                status, _, body = request("POST", f"/api/sources/{source_id}/delete", {"confirmed": True})
                self.assertEqual(status, 200, body)
                self.assertFalse(json.loads(body)["source"]["available"])
                self.assertEqual(request("GET", f"/media/source/{source_id}")[0], 404)
                status, _, after_delete = request("GET", clip["download_url"])
                self.assertEqual(status, 200)
                self.assertEqual(after_delete, video_bytes)
                self.assertEqual(request("GET", clip["caption_url"])[2], captions)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
            server = create_server(config, port=0)
            try:
                restored = server.service.library()
                self.assertEqual(restored["clips"][0]["title"], "Reviewed local clip")
                self.assertEqual(restored["clips"][0]["state"], "PREPARED")
                self.assertEqual(len(restored["sources"]), 1)
                self.assertEqual(restored["jobs"][0]["state"], "completed")
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
