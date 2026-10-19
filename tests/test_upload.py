"""Offline upload-protocol checks: no OAuth flow or real upload is performed."""
import hashlib
import json
import tempfile
import unittest
from types import ModuleType, SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from clipforge.common import DB, Settings
from clipforge import upload


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.body = body or {}
        self.headers = headers or {}

    def json(self):
        return self.body


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.outcomes:
            raise AssertionError("unexpected HTTP request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class GoogleAuthError(Exception):
    def __init__(self, *args, retryable=False):
        super().__init__(*args)
        self.retryable = retryable


class RefreshError(GoogleAuthError):
    pass


class TransportError(GoogleAuthError):
    pass


# Match the real supported exception's module for the optional-dependency branch.
RefreshError.__module__ = "google.auth.exceptions"


CHANNEL = Response(200, {"items": [{"id": "UC_TEST"}]})
URI = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=SECRET_SESSION"
START = Response(200, headers={"Location": URI})
DONE = Response(201, {"id": "uploaded_01"})


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = DB(self.root / "state.sqlite3")
        self.settings = Settings.model_validate({
            "paths": {"root": str(self.root)},
            "upload": {"enabled": True, "target_channel_id": "UC_TEST",
                       "chunk_bytes": 262144, "max_retries": 0,
                       "made_for_kids": False, "contains_synthetic_media": False}})
        video = self.root / "clip.mp4"
        video.write_bytes(b"v" * (262144 + 123))
        self.artifact = {
            "clip_id": "stable-clip", "source_sha256": "s" * 64,
            "render_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "video_path": str(video), "metadata": {"title": "Supported takeaway",
                "description": "Verified source credit.", "tags": ["tutorial"]},
            "source_manifest": {"editorial_approved": True, "metadata_approved": True,
                                "visual_approved": True, "schedule": None},
            "candidate": {"needs_visual_review": False}, "versions": {"test": "1"}}
        self.save()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def save(self):
        (self.root / "artifact.json").write_text(json.dumps(self.artifact), encoding="utf-8")

    def run_upload(self, session):
        with patch.object(upload, "_session", return_value=session), patch.object(upload, "_pause"):
            return upload.upload_prepared(self.settings, self.db, self.root, enable_upload=True)

    def test_default_prepares_without_network(self):
        with patch.object(upload, "_session") as network:
            result = upload.upload_prepared(self.settings, self.db, self.root)
        self.assertEqual(result["state"], "prepared")
        network.assert_not_called()

    def test_valid_json_with_invalid_artifact_shape_requires_review(self):
        invalid = [[], {}, {**self.artifact, "clip_id": None},
                   {**self.artifact, "video_path": 42},
                   {**self.artifact, "metadata": []},
                   {**self.artifact, "source_manifest": None},
                   {**self.artifact, "candidate": True}]
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                (self.root / "artifact.json").write_text(json.dumps(value), encoding="utf-8")
                with patch.object(upload, "_session") as network:
                    result = upload.upload_prepared(self.settings, self.db, self.root, True)
                self.assertEqual(result["state"], "needs_review")
                self.assertEqual(result["error"], "prepared_artifact_invalid_shape")
                network.assert_not_called()

    def test_interrupted_upload_resumes_without_another_insert(self):
        first = Session([CHANNEL, START, Response(308, headers={"Range": "bytes=0-262143"}),
                         KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_upload(first)
        row = self.db.conn.execute("SELECT * FROM publications").fetchone()
        self.assertEqual(row["offset_bytes"], 262144)
        self.assertEqual(row["session_uri"], URI)
        resumed = Session([CHANNEL, Response(308, headers={"Range": "bytes=0-262143"}), DONE])
        result = self.run_upload(resumed)
        self.assertEqual(result["state"], "upload_accepted")
        self.assertEqual(result["video_id"], "uploaded_01")
        self.assertEqual(sum(call[0] == "POST" for call in first.calls + resumed.calls), 1)
        self.assertEqual(resumed.calls[1][2]["headers"]["Content-Range"], "bytes */262267")
        self.assertEqual(resumed.calls[2][2]["headers"]["Content-Range"], "bytes 262144-262266/262267")
        repeated = Session([])
        self.assertEqual(self.run_upload(repeated)["video_id"], "uploaded_01")
        self.assertEqual(repeated.calls, [])

    def test_lost_final_response_is_recovered_by_session_query(self):
        session = Session([CHANNEL, START, Response(308, headers={"Range": "bytes=0-262143"}),
                           requests.Timeout("do not expose SECRET_SESSION"), DONE])
        result = self.run_upload(session)
        self.assertEqual(result["video_id"], "uploaded_01")
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)
        self.assertEqual(session.calls[-1][2]["data"], b"")
        self.assertNotIn("SECRET_SESSION", json.dumps(result))

    def test_expired_ambiguous_session_requires_reconciliation(self):
        first = Session([CHANNEL, START, KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_upload(first)
        resumed = Session([CHANNEL, Response(404)])
        result = self.run_upload(resumed)
        self.assertEqual(result["state"], "needs_reconciliation")
        self.assertEqual(sum(call[0] == "POST" for call in resumed.calls), 0)
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_reconciliation")

    def test_lost_session_creation_response_never_reinserts(self):
        first = Session([CHANNEL, requests.Timeout("SECRET_SESSION")])
        result = self.run_upload(first)
        self.assertEqual(result["state"], "needs_reconciliation")
        second = Session([])
        self.assertEqual(self.run_upload(second)["state"], "needs_reconciliation")
        self.assertEqual(second.calls, [])

    def test_unknown_completion_body_never_reinserts(self):
        session = Session([CHANNEL, START, Response(200, {"not_id": "missing"})])
        self.assertEqual(self.run_upload(session)["state"], "needs_reconciliation")
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_reconciliation")

    def test_all_bytes_acknowledged_without_completion_probes_for_id(self):
        session = Session([CHANNEL, START, Response(308, headers={"Range": "bytes=0-262266"}), DONE])
        result = self.run_upload(session)
        self.assertEqual(result["video_id"], "uploaded_01")
        self.assertEqual(session.calls[-1][2]["data"], b"")
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_definite_quota_rejection_reuses_intention_on_later_retry(self):
        quota = Response(403, {"error": {"errors": [{"reason": "quotaExceeded"}]}})
        rejected = self.run_upload(Session([CHANNEL, quota]))
        self.assertEqual(rejected["state"], "quota_wait")
        self.assertIsNotNone(rejected["next_attempt_at"])
        no_retry = Session([])
        self.assertEqual(self.run_upload(no_retry)["state"], "quota_wait")
        self.assertEqual(no_retry.calls, [])
        self.db.conn.execute("UPDATE publications SET next_attempt_at=?",
                             ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        result = self.run_upload(Session([CHANNEL, START, DONE]))
        self.assertEqual(result["state"], "upload_accepted")
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publications").fetchone()[0], 1)

    def test_verifies_target_channel_before_insert(self):
        session = Session([Response(200, {"items": [{"id": "UC_WRONG"}]})])
        result = self.run_upload(session)
        self.assertEqual(result["state"], "auth_required")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publications").fetchone()[0], 1)
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 0)

    def test_approval_and_declarations_are_required(self):
        for field in ("editorial_approved", "metadata_approved"):
            original = self.artifact["source_manifest"][field]
            self.artifact["source_manifest"][field] = False
            self.save()
            self.assertEqual(self.run_upload(Session([]))["state"], "needs_review")
            self.artifact["source_manifest"][field] = original
        self.settings.upload.made_for_kids = None
        self.save()
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_review")

    def test_visual_approval_required_when_candidate_flags_it(self):
        self.artifact["candidate"]["needs_visual_review"] = True
        self.artifact["source_manifest"]["visual_approved"] = False
        self.save()
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_review")

    def test_schedule_is_private_and_utc(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        self.artifact["source_manifest"]["schedule"] = future.isoformat()
        self.settings.upload.privacy_status = "public"
        body, schedule = upload._body(self.settings, self.artifact)
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertTrue(schedule.endswith("Z"))
        self.assertEqual(body["status"]["selfDeclaredMadeForKids"], False)
        self.assertEqual(body["status"]["containsSyntheticMedia"], False)

    def test_schedule_rejects_naive_stale_and_insufficient_lead(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        for value in ("2030-01-02T09:00:00", "2029-12-31T09:00:00Z",
                      "2030-01-01T00:30:00Z", "nonsense"):
            with self.assertRaises(ValueError):
                upload.validate_schedule(value, 60, now)
        self.assertEqual(upload.validate_schedule("2030-01-01T02:00:00-05:00", 60, now),
                         "2030-01-01T07:00:00Z")

    def test_changed_video_checksum_rejected_before_network(self):
        Path(self.artifact["video_path"]).write_bytes(b"changed")
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_review")

    def test_processing_is_polled_separately_from_upload_acceptance(self):
        self.run_upload(Session([CHANNEL, START, DONE]))
        pending = Session([CHANNEL, Response(200, {"items": [{
            "snippet": {"channelId": "UC_TEST"}, "status": {"privacyStatus": "private"},
            "processingDetails": {"processingStatus": "processing"}}]})])
        with patch.object(upload, "_session", return_value=pending):
            self.assertEqual(upload.poll_publications(self.settings, self.db)[0]["state"], "processing")
        with patch.object(upload, "_session") as network:
            self.assertEqual(upload.poll_publications(self.settings, self.db), [])
            network.assert_not_called()
        self.db.conn.execute("UPDATE publications SET last_poll_at=?",
                             ((datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(),))
        ready = Session([CHANNEL, Response(200, {"items": [{
            "snippet": {"channelId": "UC_TEST"}, "status": {"privacyStatus": "public"},
            "processingDetails": {"processingStatus": "succeeded"}}]})])
        with patch.object(upload, "_session", return_value=ready):
            self.assertEqual(upload.poll_publications(self.settings, self.db)[0]["state"], "published")

    def test_auth_failure_holds_until_credentials_change(self):
        with patch.object(upload, "_session", side_effect=upload.UploadError("auth_required", "authorize_first")):
            self.assertEqual(upload.upload_prepared(self.settings, self.db, self.root, True)["state"], "auth_required")
        with patch.object(upload, "_session") as network:
            self.assertEqual(upload.upload_prepared(self.settings, self.db, self.root, True)["state"], "auth_required")
            network.assert_not_called()
        token = self.settings.resolve(self.settings.upload.token_file)
        token.parent.mkdir(parents=True)
        token.write_text("changed credentials marker", encoding="utf-8")
        self.assertEqual(self.run_upload(Session([CHANNEL, START, DONE]))["state"], "upload_accepted")

    def test_cross_cycle_retries_are_finite_and_backed_off(self):
        self.settings.upload.max_retries = 1
        failed = Session([requests.Timeout(), requests.Timeout()])
        self.assertEqual(self.run_upload(failed)["state"], "retry_wait")
        with patch.object(upload, "_session") as network:
            self.assertEqual(upload.upload_prepared(self.settings, self.db, self.root, True)["state"], "retry_wait")
            network.assert_not_called()
        self.db.conn.execute("UPDATE publications SET next_attempt_at=?",
                             ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.run_upload(Session([requests.Timeout(), requests.Timeout()]))["state"], "needs_review")
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_review")

    def test_stale_remote_data_deleted_without_erasing_duplicate_guard(self):
        self.run_upload(Session([CHANNEL, START, DONE]))
        self.db.conn.execute("UPDATE publications SET api_refreshed_at=?",
                             ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),))
        self.assertEqual(upload.expire_owner_observations(self.db), 1)
        row = self.db.conn.execute("SELECT * FROM publications").fetchone()
        self.assertIsNone(row["video_id"])
        self.assertIsNone(row["session_uri"])
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_reconciliation")

    def test_owner_data_deletion_purges_token_and_keeps_only_hash_tombstone(self):
        self.run_upload(Session([CHANNEL, START, DONE]))
        token = self.settings.resolve(self.settings.upload.token_file)
        token.parent.mkdir(parents=True)
        token.write_text('{"refresh_token":"secret"}', encoding="utf-8")
        result = upload.delete_owner_upload_data(self.settings, self.db)
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["youtube_videos_deleted"])
        self.assertFalse(token.exists())
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publications").fetchone()[0], 0)
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publication_tombstones").fetchone()[0], 1)
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_reconciliation")

    def test_rejected_refresh_token_removes_owner_api_data_immediately(self):
        self.run_upload(Session([CHANNEL, START, DONE]))
        with patch.object(upload, "_session", side_effect=upload.UploadError("auth_required", "refresh_token_rejected")):
            result = upload.poll_publications(self.settings, self.db)
        self.assertEqual(result[0]["error"], "consent_data_deleted_reauthorize")
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publications").fetchone()[0], 0)

    def test_completed_records_refreshed_before_thirty_days(self):
        self.run_upload(Session([CHANNEL, START, DONE]))
        previous = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
        self.db.conn.execute("UPDATE publications SET publication_state='published',last_poll_at=?,api_refreshed_at=?",
                             (previous, previous))
        ready = Session([CHANNEL, Response(200, {"items": [{
            "snippet": {"channelId": "UC_TEST"}, "status": {"privacyStatus": "public"},
            "processingDetails": {"processingStatus": "succeeded"}}]})])
        with patch.object(upload, "_session", return_value=ready):
            self.assertEqual(upload.poll_publications(self.settings, self.db)[0]["state"], "published")
        row = self.db.conn.execute("SELECT * FROM publications").fetchone()
        self.assertGreater(upload._date(row["api_refreshed_at"]), datetime.now(timezone.utc) - timedelta(minutes=1))

    def test_superseded_artifact_cannot_start_upload(self):
        self.artifact["state"] = "SUPERSEDED"
        self.save()
        self.assertEqual(self.run_upload(Session([]))["state"], "needs_review")

    def test_long_retry_after_persists_cooldown_without_blocking_sleep(self):
        self.settings.upload.max_retries = 2
        response = Response(429, headers={"Retry-After": "3600"})
        with patch.object(upload.time, "sleep") as sleep:
            with self.assertRaises(upload.UploadError) as raised:
                upload._pause(0, response)
            sleep.assert_not_called()
        self.assertGreater(raised.exception.retry_after, datetime.now(timezone.utc) + timedelta(minutes=59))
        failed = self.run_upload(Session([CHANNEL, response]))
        self.assertEqual(failed["state"], "retry_wait")
        self.assertGreater(upload._date(failed["next_attempt_at"]), datetime.now(timezone.utc) + timedelta(minutes=59))

    def test_explicit_revocation_calls_documented_endpoint_and_purges(self):
        self.run_upload(Session([CHANNEL, START, DONE]))
        token = self.settings.resolve(self.settings.upload.token_file)
        token.parent.mkdir(parents=True)
        token.write_text('{"refresh_token":"test-only-token"}', encoding="utf-8")
        with patch.object(upload.requests, "post", return_value=Response(200)) as revoke:
            result = upload.revoke_owner_consent(self.settings, self.db)
        self.assertTrue(result["revocation_confirmed"])
        self.assertEqual(revoke.call_args.args[0], "https://oauth2.googleapis.com/revoke")
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publications").fetchone()[0], 0)

    def test_retryable_refresh_error_during_request_preserves_session_and_token(self):
        self.settings.upload.max_retries = 2
        token = self.settings.resolve(self.settings.upload.token_file)
        token.parent.mkdir(parents=True)
        token.write_text("keep credentials", encoding="utf-8")
        result = self.run_upload(Session([CHANNEL, START, RefreshError("temporary", retryable=True)]))
        self.assertEqual(result["state"], "retry_wait")
        self.assertEqual(result["error"], "token_refresh_transient_failure")
        self.assertEqual(token.read_text(encoding="utf-8"), "keep credentials")
        row = self.db.conn.execute("SELECT * FROM publications").fetchone()
        self.assertEqual(row["session_uri"], URI)
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publication_tombstones").fetchone()[0], 0)

    def test_retryable_credential_refresh_error_preserves_publication_and_token(self):
        self.settings.upload.max_retries = 2
        self.run_upload(Session([CHANNEL, START, DONE]))
        token = self.settings.resolve(self.settings.upload.token_file)
        token.parent.mkdir(parents=True)
        token.write_text("keep credentials", encoding="utf-8")

        def fail_refresh(request):
            raise RefreshError("temporary endpoint failure", retryable=True)

        credentials = SimpleNamespace(valid=False, refresh_token="test", has_scopes=lambda scopes: True,
                                      refresh=fail_refresh)
        exceptions = ModuleType("google.auth.exceptions")
        exceptions.GoogleAuthError = GoogleAuthError
        exceptions.RefreshError = RefreshError
        exceptions.TransportError = TransportError
        transport = ModuleType("google.auth.transport.requests")
        transport.Request = lambda: object()
        transport.AuthorizedSession = lambda *args, **kwargs: None
        oauth = ModuleType("google.oauth2.credentials")
        oauth.Credentials = SimpleNamespace(from_authorized_user_file=lambda *args: credentials)
        modules = {"google.auth.exceptions": exceptions, "google.auth.transport.requests": transport,
                   "google.oauth2.credentials": oauth}
        with patch.dict("sys.modules", modules):
            result = upload.poll_publications(self.settings, self.db)
        self.assertEqual(result[0]["state"], "retry_wait")
        self.assertEqual(result[0]["error"], "token_refresh_transient_failure")
        self.assertEqual(token.read_text(encoding="utf-8"), "keep credentials")
        row = self.db.conn.execute("SELECT * FROM publications").fetchone()
        self.assertEqual(row["video_id"], "uploaded_01")
        self.assertEqual(row["session_uri"], URI)
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publication_tombstones").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
