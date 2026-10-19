import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from clipforge.brain import ReviewRequired
from clipforge.common import (DB, Candidate, Metadata, Settings, SourceManifest, Span,
                              Transcript, Word, atomic_json, run_command, sha256_file, stable_id)
from clipforge.worker import (append_credits, approve_artifact, daily_slots,
                              prepare_manifest, reserve_slot, verify_artifact)


def manifest_fixture(**updates):
    values = {"media_path": "source.mp4", "rights_category": "original",
              "permission_reference": "Generated original synthetic source.",
              "creator_credit": "Verified creator: synthetic fixture.",
              "contribution": "Demonstration of original synthetic video assembly.",
              "editorial_approved": True, "metadata_approved": True,
              "transcript_path": "transcript.json", "candidates_path": "candidates.json",
              "metadata_path": "metadata.json"}
    values.update(updates)
    return values


class SlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.temp.name) / "state.sqlite3")
        self.now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        self.clock = patch("clipforge.worker.utcnow", return_value=self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.db.close()
        self.temp.cleanup()

    def test_reservation_retries_reuse_original_utc_slot(self):
        one, two = self.now + timedelta(days=1), self.now + timedelta(days=2)
        initial = reserve_slot(self.db, "channel-A", "clip-1", [one, two])
        retry = reserve_slot(self.db, "channel-A", "clip-1", [two])
        self.assertEqual(initial, retry)
        self.assertEqual(reserve_slot(self.db, "channel-A", "clip-2", [one, two]), two.isoformat())
        self.assertEqual(reserve_slot(self.db, "channel-B", "clip-1", [one]), one.isoformat())
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM slots").fetchone()[0], 3)

    def test_past_naive_and_exhausted_slots_cannot_be_reserved(self):
        for value in (self.now - timedelta(seconds=1), self.now.replace(tzinfo=None)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reserve_slot(self.db, "channel-A", "clip-1", [value])
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM slots").fetchone()[0], 0)
        with self.assertRaises(ReviewRequired):
            reserve_slot(self.db, "channel-A", "clip-1", [])

    def test_dst_ambiguous_and_nonexistent_local_slots_are_skipped(self):
        with patch("clipforge.worker.utcnow", return_value=datetime(2026, 10, 31, 12, tzinfo=timezone.utc)):
            fall = daily_slots("America/New_York", 1, days=4)
        self.assertFalse(any(slot.date().isoformat() == "2026-11-01" for slot in fall))
        with patch("clipforge.worker.utcnow", return_value=datetime(2026, 3, 7, 12, tzinfo=timezone.utc)):
            spring = daily_slots("America/New_York", 2, days=4)
        self.assertFalse(any(slot.date().isoformat() == "2026-03-08" for slot in spring))

    def test_credit_and_music_attribution_are_appended_verbatim(self):
        manifest = SourceManifest.model_validate(manifest_fixture(music_attribution="Exact attribution © 2026.\nSecond line."))
        metadata = Metadata(title="Original example", description="Supported takeaway.", tags=["example"])
        final = append_credits(metadata, manifest)
        self.assertTrue(final.description.endswith(manifest.creator_credit + "\n" + manifest.music_attribution))


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG") or shutil.which("ffmpeg")
        cls.ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE") or shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg + ffprobe required for synthetic full preparation")
        cls.temp_source = tempfile.TemporaryDirectory(prefix="clipforge-worker-fixture-")
        cls.source = Path(cls.temp_source.name) / "original.mp4"
        run_command([cls.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                     "testsrc2=size=160x90:rate=5:duration=22", "-f", "lavfi", "-i",
                     "sine=frequency=440:sample_rate=48000:duration=22", "-shortest", "-c:v",
                     "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.source)])

    @classmethod
    def tearDownClass(cls):
        cls.temp_source.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clipforge-worker-")
        self.root = Path(self.temp.name)
        self.settings = Settings()
        self.settings.paths.root = str(self.root)
        self.settings.render.ffmpeg, self.settings.render.ffprobe = self.ffmpeg, self.ffprobe
        self.settings.render.width, self.settings.render.height = 180, 320
        self.settings.render.fps, self.settings.render.preset = 5, "ultrafast"
        self.settings.render.font_size, self.settings.render.margin_v = 12, 32
        self.db = DB(self.root / "state.sqlite3")
        shutil.copyfile(self.source, self.root / "source.mp4")
        transcript = Transcript(language="en", duration_s=22.0,
                                words=[Word(start_s=0.4+i, end_s=0.9+i, text=f"Original word {i}.") for i in range(20)],
                                spans=[Span(id="s0", start_s=0.4, end_s=10.4, text="Original hook."),
                                       Span(id="s1", start_s=10.4, end_s=20.4, text="Original payoff.")])
        candidate = Candidate(start_id="s0", end_id="s1", start_s=0.4, end_s=20.4,
                              hook_id="s0", payoff_id="s1", score=0.8,
                              reason="Synthetic fixture.", needs_visual_review=False)
        atomic_json(self.root / "transcript.json", transcript.model_dump())
        atomic_json(self.root / "candidates.json", {"clips": [candidate.model_dump()]})
        atomic_json(self.root / "metadata.json", {"title": "An original synthetic example",
                                                 "description": "This clip demonstrates synthetic video assembly.",
                                                 "tags": ["synthetic video"]})
        self.manifest = self.root / "manifest.json"
        atomic_json(self.manifest, manifest_fixture())

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def prepare(self, **changes):
        if changes:
            atomic_json(self.manifest, manifest_fixture(**changes))
        return prepare_manifest(self.settings, self.db, self.manifest)

    def test_full_preparation_and_repeat_run_without_models_or_duplicate_artifacts(self):
        with patch("clipforge.worker.LocalLLM", side_effect=AssertionError("manual fixture needs no model")):
            first = self.prepare()
        self.assertEqual(first["state"], "PREPARED")
        folder = Path(first["artifacts"][0])
        verified = verify_artifact(self.settings, folder)
        self.assertEqual(verified["decode"], "ok")
        artifact = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
        self.assertTrue(artifact["metadata"]["description"].endswith("Verified creator: synthetic fixture."))
        video = Path(artifact["video_path"])
        original_time = video.stat().st_mtime_ns
        with patch("clipforge.worker.transcribe", side_effect=AssertionError("repeat must use preparation cache")):
            second = self.prepare()
        self.assertTrue(second["cached"])
        self.assertEqual(second["artifacts"], first["artifacts"])
        self.assertEqual(video.stat().st_mtime_ns, original_time)
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM artifacts").fetchone()[0], 1)

    def test_missing_media_waits_then_resumes_when_authorized_source_arrives(self):
        missing = self.root / "incoming.mp4"
        initial = self.prepare(media_path=str(missing))
        self.assertEqual(initial["state"], "WAITING_MEDIA")
        shutil.copyfile(self.source, missing)
        resumed = prepare_manifest(self.settings, self.db, self.manifest)
        self.assertEqual(resumed["state"], "PREPARED")

    def test_changed_selection_retires_superseded_preparations(self):
        self.settings.brain.min_clip_s = 9.0
        initial = self.prepare()
        old_folder = Path(initial["artifacts"][0])
        candidates = json.loads((self.root / "candidates.json").read_text(encoding="utf-8"))
        candidates["clips"][0].update(start_id="s1", start_s=10.4, hook_id="s1")
        atomic_json(self.root / "candidates.json", candidates)
        replacement = self.prepare()
        self.assertEqual(replacement["state"], "PREPARED")
        self.assertNotEqual(initial["artifacts"], replacement["artifacts"])
        repeated = self.prepare()
        self.assertEqual(repeated["artifacts"], replacement["artifacts"],
                         "cached jobs must not resurrect a previous selection")
        if (old_folder / "artifact.json").exists():
            previous = json.loads((old_folder / "artifact.json").read_text(encoding="utf-8"))
            self.assertNotEqual(previous["state"], "PREPARED",
                                "publication scans must not publish a superseded approved excerpt")

    def test_empty_replacement_does_not_keep_an_old_approved_excerpt_active(self):
        initial = self.prepare()
        old_folder = Path(initial["artifacts"][0])
        atomic_json(self.root / "candidates.json", {"clips": []})
        replacement = self.prepare()
        self.assertEqual(replacement["state"], "NO_CANDIDATES")
        repeated = self.prepare()
        self.assertEqual(repeated["state"], "NO_CANDIDATES")
        self.assertEqual(repeated["artifacts"], [])
        if (old_folder / "artifact.json").exists():
            previous = json.loads((old_folder / "artifact.json").read_text(encoding="utf-8"))
            self.assertNotEqual(previous["state"], "PREPARED")

    def test_approval_requires_metadata_and_flagged_visual_review(self):
        candidates = json.loads((self.root / "candidates.json").read_text(encoding="utf-8"))
        candidates["clips"][0]["needs_visual_review"] = True
        atomic_json(self.root / "candidates.json", candidates)
        result = self.prepare(editorial_approved=False, metadata_approved=False)
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        folder = Path(result["artifacts"][0])
        with self.assertRaisesRegex(ReviewRequired, "metadata"):
            approve_artifact(folder, visual_reviewed=True, metadata_reviewed=False)
        with self.assertRaisesRegex(ReviewRequired, "visual"):
            approve_artifact(folder, visual_reviewed=False, metadata_reviewed=True)
        approve_artifact(folder, visual_reviewed=True, metadata_reviewed=True)
        data = json.loads((folder / "artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(data["state"], "PREPARED")
        self.assertTrue(data["source_manifest"]["metadata_approved"])
        self.assertTrue(data["source_manifest"]["visual_approved"])
        # The cached job status should follow its now-approved artifacts.
        self.assertEqual(self.prepare()["state"], "PREPARED")

    def test_invalid_manifest_and_supplied_schemas_require_review(self):
        for kind in ("manifest", "candidate", "metadata"):
            with self.subTest(kind=kind):
                atomic_json(self.manifest, manifest_fixture(editorial_approved="yes")
                            if kind == "manifest" else manifest_fixture())
                if kind == "candidate":
                    candidates = json.loads((self.root / "candidates.json").read_text(encoding="utf-8"))
                    candidates["clips"][0]["invented_field"] = True
                    atomic_json(self.root / "candidates.json", candidates)
                elif kind == "metadata":
                    # Reset the supplied candidate after the preceding subtest.
                    candidates["clips"][0].pop("invented_field", None)
                    atomic_json(self.root / "candidates.json", candidates)
                    atomic_json(self.root / "metadata.json", {"title": "Example", "description": "Supported.",
                                                             "tags": [], "unsupported_extra": True})
                result = prepare_manifest(self.settings, self.db, self.manifest)
                self.assertEqual(result["state"], "REVIEW_REQUIRED")

    def test_invented_timestamp_cannot_render(self):
        candidates = json.loads((self.root / "candidates.json").read_text(encoding="utf-8"))
        candidates["clips"][0]["start_s"] = 0.40001
        atomic_json(self.root / "candidates.json", candidates)
        with patch("clipforge.factory.render_clip", side_effect=AssertionError("invalid timestamp must not render")):
            self.assertEqual(self.prepare()["state"], "REVIEW_REQUIRED")

    def test_transient_failures_have_finite_attempts_and_backoff(self):
        with patch("clipforge.worker.probe_source", side_effect=OSError("synthetic transient failure")):
            first = self.prepare()
            self.assertEqual(first["state"], "RETRY_WAIT")
            self.assertEqual(self.prepare()["state"], "RETRY_WAIT")
            self.assertEqual(self.db.conn.execute("SELECT attempts FROM jobs").fetchone()[0], 1)
            for expected in ("RETRY_WAIT", "FAILED"):
                self.db.conn.execute("UPDATE jobs SET updated_at=?", ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
                self.assertEqual(self.prepare()["state"], expected)
            self.assertTrue(self.prepare()["retry_limit"])
        self.assertEqual(self.db.conn.execute("SELECT attempts FROM jobs").fetchone()[0], 3)

    def test_submitted_same_identity_cannot_be_regenerated_by_changed_inputs_or_retry(self):
        from clipforge import upload

        initial = self.prepare()
        folder = Path(initial["artifacts"][0])
        original_artifact = (folder / "artifact.json").read_bytes()
        artifact = json.loads(original_artifact)
        video = Path(artifact["video_path"])
        original_video_hash, original_mtime = sha256_file(video), video.stat().st_mtime_ns
        self.settings.upload.enabled = True
        self.settings.upload.target_channel_id = "UC_SYNTHETIC_TEST"
        self.settings.upload.made_for_kids = False
        self.settings.upload.contains_synthetic_media = False
        # Exercise the real durable-intention writer. Authorization fails before
        # any HTTP request; the resulting publication row is still immutable.
        with patch("clipforge.upload._session", side_effect=upload.UploadError("auth_required", "authorize_first")):
            submitted = upload.upload_prepared(self.settings, self.db, folder, enable_upload=True)
        self.assertEqual(submitted["state"], "auth_required")
        publication = dict(self.db.conn.execute("SELECT * FROM publications").fetchone())
        self.assertEqual(publication["clip_id"], artifact["clip_id"])
        self.assertEqual(publication["render_sha256"], original_video_hash)
        atomic_json(self.manifest, manifest_fixture(creator_credit="A changed creator-credit record."))
        atomic_json(self.root / "metadata.json", {"title": "A changed supported title",
                                                 "description": "A new description must not replace a submitted artifact.",
                                                 "tags": ["updated"]})
        with patch("clipforge.worker.LocalLLM", side_effect=AssertionError("no model is needed")), \
             patch("clipforge.worker.generate_metadata", side_effect=AssertionError("submitted metadata is immutable")), \
             patch("clipforge.factory.render_clip", side_effect=AssertionError("submitted render is immutable")):
            for retry in (False, True):
                with self.subTest(explicit_retry=retry):
                    result = prepare_manifest(self.settings, self.db, self.manifest, retry=retry)
                    self.assertEqual(result["artifacts"], initial["artifacts"])
                    self.assertEqual((folder / "artifact.json").read_bytes(), original_artifact)
                    self.assertEqual(sha256_file(video), original_video_hash)
                    self.assertEqual(video.stat().st_mtime_ns, original_mtime)
        unchanged = dict(self.db.conn.execute("SELECT * FROM publications").fetchone())
        self.assertEqual(unchanged["artifact_json"], publication["artifact_json"])
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM publications").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
