"""Mocked official provider protocols; these tests never send a real post."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from clipforge.common import DB, Settings, atomic_json, sha256_file
from clipforge import social_publish as social


def response(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data)


def tt(data):
    return response({"data": data, "error": {"code": "ok"}})


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        saved = dict(kwargs)
        if hasattr(saved.get("data"), "read"):
            saved["data"] = saved["data"].read()
        self.calls.append((method, url, saved))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class SocialPublishingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings()
        self.settings.paths.root = str(self.root)
        self.settings.instagram.enabled = self.settings.tiktok.enabled = True
        self.settings.instagram.target_account_id = "12345"
        self.db = DB(self.root / "state.sqlite3")
        social.schema(self.db)
        atomic_json(self.root / "secrets" / "instagram.json", {"access_token": "instagram-test-secret",
            "account_id": "12345", "login_type": "facebook_business", "expires_at": "2099-01-01T00:00:00Z"})
        atomic_json(self.root / "secrets" / "tiktok.json", {"access_token": "tiktok-test-secret", "open_id": "authorized-open-id",
            "scope": "video.publish", "client_audited": True, "expires_at": "2099-01-01T00:00:00Z"})
        self.folder = self.root / "prepared" / "clip"
        self.folder.mkdir(parents=True)
        self.video = self.folder / "clip.mp4"
        self.video.write_bytes(b"test video bytes")
        self.artifact = {"clip_id": "clip-123", "state": "PREPARED", "source_sha256": "owned-source-sha",
            "video_path": str(self.video), "render_sha256": sha256_file(self.video),
            "candidate": {"start_s": 1.0, "end_s": 8.0, "needs_visual_review": True},
            "metadata": {"title": "Owned clip", "description": "Reviewed description", "tags": []},
            "source_manifest": {"editorial_approved": True, "metadata_approved": True, "visual_approved": True}}
        atomic_json(self.folder / "artifact.json", self.artifact)
        self.ig_options = {"confirmed": True, "caption": "My reviewed Reel", "share_to_feed": True, "account_id": "12345"}
        self.tt_options = {"confirmed": True, "caption": "My original clip", "privacy_level": "SELF_ONLY",
            "disable_comment": True, "disable_duet": True, "disable_stitch": True,
            "brand_content_toggle": False, "brand_organic_toggle": False, "is_aigc": False,
            "creator_username": "actual_creator", "music_usage_confirmed": True}

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def ig_creator(self, account="12345"):
        return response({"id": "page-1", "instagram_business_account": {"id": account, "username": "actual_creator"}})

    def tt_creator(self, **changes):
        return tt(dict({"creator_username": "actual_creator", "creator_nickname": "Actual Creator",
            "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"], "comment_disabled": False,
            "duet_disabled": False, "stitch_disabled": True, "max_video_post_duration_sec": 300}, **changes))

    def test_local_readiness_and_expiry_never_make_network_requests(self):
        with patch("clipforge.social_publish._session") as session:
            self.assertTrue(social.provider_status(self.settings, "instagram")["ok"])
            atomic_json(self.root / "secrets" / "tiktok.json", {"access_token": "expired-test-secret-value", "open_id": "me", "scope": "video.publish", "expires_at": "2000-01-01T00:00:00Z"})
            status = social.provider_status(self.settings, "tiktok")
            self.assertFalse(status["ok"])
            self.assertIn("expired", status["message"])
            self.assertNotIn("expired-test-secret-value", status["message"])
            session.assert_not_called()

    def test_strict_consent_privacy_and_utf16_caption_validation(self):
        for changes in ({"confirmed": False}, {"music_usage_confirmed": False}, {"privacy_level": ""},
                        {"disable_comment": "true"}, {"brand_content_toggle": True}, {"caption": "😀" * 1101}):
            with self.subTest(changes=list(changes)), self.assertRaises(social.SocialError):
                social.validate_options("tiktok", dict(self.tt_options, **changes))
        with self.assertRaises(social.SocialError):
            social.validate_options("instagram", dict(self.ig_options, secret="never accepted"))

    def test_tiktok_real_file_upload_chunk_ranges_and_status_are_safe(self):
        self.video.write_bytes(b"v" * 25_000_000)
        self.artifact["render_sha256"] = sha256_file(self.video)
        atomic_json(self.folder / "artifact.json", self.artifact)
        http = FakeHTTP([self.tt_creator(), tt({"publish_id": "v_pub_file~v2.123", "upload_url":
            "https://open-upload.tiktokapis.com/video/?upload_token=upload-secret"}), response({}, 206), response({}, 201)])
        with patch("clipforge.social_publish._session", return_value=http):
            saved = social.publish(self.settings, self.db, self.folder, "tiktok", self.tt_options)
            self.assertEqual(saved["state"], "processing")
            count = len(http.calls)
            social.publish(self.settings, self.db, self.folder, "tiktok", self.tt_options)
            self.assertEqual(len(http.calls), count, "existing intent must never create another post")
        initialize = http.calls[1][2]["json"]
        self.assertEqual(initialize["source_info"], {"source": "FILE_UPLOAD", "video_size": 25_000_000, "chunk_size": 10_000_000, "total_chunk_count": 2})
        self.assertEqual(http.calls[2][2]["headers"]["Content-Range"], "bytes 0-9999999/25000000")
        self.assertEqual(http.calls[3][2]["headers"]["Content-Range"], "bytes 10000000-24999999/25000000")
        for _, _, kwargs in http.calls[2:]:
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertFalse(kwargs["allow_redirects"])
        serialized = json.dumps(social.public_records(self.db, "clip-123"))
        self.assertNotIn("upload-secret", serialized)
        self.assertNotIn("tiktok-test-secret", serialized)
        self.assertNotIn("video_path", serialized)
        http = FakeHTTP([self.tt_creator(), tt({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [123456]})])
        with patch("clipforge.social_publish._session", return_value=http):
            published = social.refresh(self.settings, self.db, "clip-123", "tiktok")
        self.assertEqual(published["state"], "published")
        self.assertEqual(published["remote_media_id"], "123456")

    def test_instagram_upload_process_publish_only_once(self):
        http = FakeHTTP([self.ig_creator(), response({"id": "777", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/777"}),
            response({"success": True}), response({"status_code": "FINISHED"}), response({"id": "888"})])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
            self.assertEqual(result["state"], "published")
            count = len(http.calls)
            social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
            self.assertEqual(len(http.calls), count)
        self.assertEqual(http.calls[1][2]["data"]["upload_type"], "resumable")
        self.assertNotIn("video_url", http.calls[1][2]["data"])
        self.assertEqual(http.calls[2][2]["headers"]["Authorization"], "OAuth instagram-test-secret")
        self.assertEqual(http.calls[2][2]["data"], self.video.read_bytes())
        self.assertEqual(sum(url.endswith("/media_publish") for _, url, _ in http.calls), 1)

    def test_instagram_deferred_finish_never_recreates_container(self):
        http = FakeHTTP([self.ig_creator(), response({"id": "777", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/777"}),
            response({"success": True}), response({"status_code": "IN_PROGRESS"})])
        with patch("clipforge.social_publish._session", return_value=http), patch("clipforge.social_publish.IG_POLL_COUNT", 1):
            self.assertEqual(social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)["state"], "processing")
        http = FakeHTTP([self.ig_creator(), response({"status_code": "FINISHED"})])
        with patch("clipforge.social_publish._session", return_value=http):
            ready = social.refresh(self.settings, self.db, "clip-123", "instagram")
        self.assertTrue(ready["can_finish"])
        self.assertEqual([method for method, _, _ in http.calls], ["GET", "GET"])
        http = FakeHTTP([self.ig_creator(), response({"id": "888"})])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.finish(self.settings, self.db, self.folder, "instagram")
        self.assertEqual(result["state"], "published")
        self.assertEqual([url.rsplit("/", 1)[-1] for _, url, _ in http.calls], ["me", "media_publish"])

    def test_lost_instagram_publish_response_is_never_replayed(self):
        http = FakeHTTP([self.ig_creator(), response({"id": "777", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/777"}),
            response({"success": True}), response({"status_code": "FINISHED"}), requests.Timeout("secret URL must not leak")])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
        self.assertEqual(result["state"], "needs_reconciliation")
        self.assertNotIn("secret URL", json.dumps(result))
        http = FakeHTTP([self.ig_creator(), response({"status_code": "FINISHED"})])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.refresh(self.settings, self.db, "clip-123", "instagram")
        self.assertFalse(result["can_finish"])
        self.assertEqual(result["state"], "needs_reconciliation")
        with self.assertRaises(social.SocialError):
            social.finish(self.settings, self.db, self.folder, "instagram")
        with patch("clipforge.social_publish._session") as never:
            social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
            never.assert_not_called()

    def test_lost_create_response_and_unsafe_upload_destination_never_retry(self):
        http = FakeHTTP([self.tt_creator(), requests.Timeout("credential-bearing URL")])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.publish(self.settings, self.db, self.folder, "tiktok", self.tt_options)
        self.assertEqual(result["state"], "needs_reconciliation")
        with patch("clipforge.social_publish._session") as never:
            social.publish(self.settings, self.db, self.folder, "tiktok", self.tt_options)
            never.assert_not_called()
        for uri in ("http://open-upload.tiktokapis.com/video/", "https://127.0.0.1/video/", "https://evil.example/video/",
                    "https://open-upload.tiktokapis.com@evil.example/video/", "https://open-upload.tiktokapis.com:8000/video/"):
            self.assertFalse(social._safe_upload_url(uri, "tiktok"))
        self.assertFalse(social._safe_upload_url("https://rupload.facebook.com/ig-api-upload/v25.0/999", "instagram", "777"))

    def test_creator_account_options_and_review_are_checked_before_remote_create(self):
        http = FakeHTTP([self.tt_creator(stitch_disabled=True)])
        with patch("clipforge.social_publish._session", return_value=http), self.assertRaises(social.SocialError):
            social.publish(self.settings, self.db, self.folder, "tiktok", dict(self.tt_options, disable_stitch=False))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(social.public_records(self.db, "clip-123"), {})
        http = FakeHTTP([self.ig_creator(account="different-account")])
        with patch("clipforge.social_publish._session", return_value=http), self.assertRaises(social.SocialError):
            social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
        self.artifact["web_is_demo"] = True
        atomic_json(self.folder / "artifact.json", self.artifact)
        with patch("clipforge.social_publish._session") as never, self.assertRaises(social.SocialError):
            social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
        never.assert_not_called()

    def test_saved_intent_rejects_edited_artifact_and_keeps_original_snapshot(self):
        http = FakeHTTP([self.tt_creator(), tt({"publish_id": "v_pub_file~v2.123", "upload_url":
            "https://open-upload.tiktokapis.com/video/?upload_token=secret"}), response({}, 201)])
        with patch("clipforge.social_publish._session", return_value=http):
            social.publish(self.settings, self.db, self.folder, "tiktok", self.tt_options)
        original = social._record(self.db, "clip-123", "tiktok")["artifact_json"]
        self.artifact["metadata"]["title"] = "Changed after posting"
        atomic_json(self.folder / "artifact.json", self.artifact)
        with patch("clipforge.social_publish._session") as never, self.assertRaises(social.SocialError):
            social.publish(self.settings, self.db, self.folder, "tiktok", self.tt_options)
        never.assert_not_called()
        self.assertEqual(social._record(self.db, "clip-123", "tiktok")["artifact_json"], original)

    def test_instagram_server_error_after_publish_does_not_allow_replay(self):
        http = FakeHTTP([self.ig_creator(), response({"id": "777", "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/777"}),
            response({"success": True}), response({"status_code": "FINISHED"}), response({"error": {"code": 1}}, 500)])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.publish(self.settings, self.db, self.folder, "instagram", self.ig_options)
        self.assertEqual(result["state"], "needs_reconciliation")
        http = FakeHTTP([self.ig_creator(), response({"status_code": "FINISHED"})])
        with patch("clipforge.social_publish._session", return_value=http):
            result = social.refresh(self.settings, self.db, "clip-123", "instagram")
        self.assertFalse(result["can_finish"])
        self.assertEqual(result["state"], "needs_reconciliation")


if __name__ == "__main__":
    unittest.main()
