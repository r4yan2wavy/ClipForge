import array
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from pydantic import ValidationError

from clipforge.brain import (LocalLLM, ReviewRequired, check_speech_runtime, extract_audio, load_subtitles, select_clips,
                             transcript_windows, validate_candidates)
from clipforge.common import (BrainConfig, Candidate, CandidateList, Settings, Span,
                              Transcript, Word, run_command)


def transcript_fixture():
    return Transcript(language="en", duration_s=80.0,
                      words=[Word(start_s=float(i*10), end_s=float((i+1)*10), text=f"Spoken {i}.")
                             for i in range(8)],
                      spans=[Span(id=f"s{i}", start_s=float(i*10), end_s=float((i+1)*10),
                                  text=f"Spoken {i}.") for i in range(8)])


def candidate_fixture(**updates):
    value = {"start_id": "s0", "end_id": "s1", "start_s": 0.0, "end_s": 20.0,
             "hook_id": "s0", "payoff_id": "s1", "score": 0.8,
             "reason": "Self-contained fixture.", "needs_visual_review": False}
    value.update(updates)
    return value


def response(value, status=200):
    result = Mock(status_code=status)
    result.json.return_value = value
    return result


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.settings, self.transcript = Settings(), transcript_fixture()

    def validate(self, **updates):
        data = CandidateList.model_validate({"clips": [candidate_fixture(**updates)]})
        return validate_candidates(data, self.transcript, self.settings)

    def test_only_existing_exact_canonical_boundaries_are_accepted(self):
        self.assertEqual(self.validate()[0].end_s, 20.0)
        for changes in ({"start_id": "invented"}, {"hook_id": "missing"},
                        {"start_s": 0.000001}, {"end_s": 19.999999}):
            with self.subTest(changes=changes), self.assertRaises(ReviewRequired):
                self.validate(**changes)

    def test_reversed_intervals_and_outside_or_reversed_anchors_are_rejected(self):
        for changes in ({"start_id": "s2", "end_id": "s1", "start_s": 20.0},
                        {"hook_id": "s2"}, {"payoff_id": "s2"},
                        {"hook_id": "s1", "payoff_id": "s0"}):
            with self.subTest(changes=changes), self.assertRaises(ReviewRequired):
                self.validate(**changes)

    def test_editing_duration_and_window_membership_are_enforced(self):
        for changes in ({"end_id": "s0", "end_s": 10.0, "payoff_id": "s0"},
                        {"end_id": "s6", "end_s": 70.0}):
            with self.subTest(changes=changes), self.assertRaises(ReviewRequired):
                self.validate(**changes)
        data = CandidateList.model_validate({"clips": [candidate_fixture()]})
        with self.assertRaisesRegex(ReviewRequired, "out-of-window"):
            validate_candidates(data, self.transcript, self.settings, {"s0"})

    def test_nonfinite_numbers_and_unexpected_fields_fail_schema(self):
        for field in ("start_s", "end_s", "score"):
            for number in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(field=field, number=number), self.assertRaises(ValidationError):
                    Candidate.model_validate(candidate_fixture(**{field: number}))
        for changes in ({"retention": 0.99}, {"needs_visual_review": "false"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Candidate.model_validate(candidate_fixture(**changes))
        with self.assertRaises(ValidationError):
            CandidateList.model_validate({"clips": [], "commentary": "extra"})

    def test_global_selection_suppresses_overlapping_window_duplicates(self):
        data = {"clips": [candidate_fixture(score=0.6), candidate_fixture(score=0.9),
                           candidate_fixture(start_id="s3", end_id="s4", start_s=30.0,
                                             end_s=50.0, hook_id="s3", payoff_id="s4", score=0.8)]}
        with tempfile.TemporaryDirectory() as directory:
            supplied = Path(directory) / "candidates.json"
            supplied.write_text(json.dumps(data), encoding="utf-8")
            chosen = select_clips(self.settings, self.transcript, None, supplied)
        self.assertEqual([(c.start_s, c.score) for c in chosen], [(0.0, 0.9), (30.0, 0.8)])

    def test_creator_srt_retains_absolute_phrase_times(self):
        with tempfile.TemporaryDirectory() as directory:
            supplied = Path(directory) / "creator.srt"
            supplied.write_text("1\n00:02:05,400 --> 00:02:10,000\nOriginal phrase.\n", encoding="utf-8")
            transcript = load_subtitles(supplied, 140.0, "en")
        self.assertEqual(transcript.spans[0].start_s, 125.4)
        self.assertEqual(len(transcript.words), 1, "SRT must not invent per-word alignments")


class WindowTests(unittest.TestCase):
    def test_windows_overlap_keep_absolute_times_and_respect_serialized_budget(self):
        spans = [Span(id=f"s{i}", start_s=100.0+i*2, end_s=102.0+i*2, text="A"*100)
                 for i in range(30)]
        transcript = Transcript(language="en", duration_s=170.0, spans=spans,
                                words=[Word(start_s=s.start_s, end_s=s.end_s, text=s.text) for s in spans])
        settings = Settings()
        settings.brain.window_chars = 1000
        windows = transcript_windows(transcript, settings)
        self.assertGreater(len(windows), 1)
        self.assertEqual({s.id for w in windows for s in w}, {s.id for s in spans})
        for window in windows:
            serialized = json.dumps([s.model_dump() for s in window], ensure_ascii=False).encode()
            self.assertLessEqual(len(serialized), 1000)
            self.assertGreaterEqual(window[0].start_s, 100.0)
        for first, second in zip(windows, windows[1:]):
            self.assertTrue({s.id for s in first} & {s.id for s in second})
            self.assertGreater(second[0].start_s, first[0].start_s)

    def test_oversized_indivisible_span_routes_to_review(self):
        transcript = Transcript(language="en", duration_s=30.0,
                                spans=[Span(id="s0", start_s=0.0, end_s=30.0, text="x"*5000)],
                                words=[Word(start_s=0.0, end_s=30.0, text="x"*5000)])
        settings = Settings()
        settings.brain.window_chars = 1000
        with self.assertRaisesRegex(ReviewRequired, "span exceeds"):
            transcript_windows(transcript, settings)


class LocalInferenceBoundaryTests(unittest.TestCase):
    def test_hosted_endpoints_cloud_models_and_proxy_credentials_are_refused(self):
        for url in ("https://example.com", "http://192.168.1.5:11434", "http://user@localhost:11434"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                BrainConfig(ollama_url=url)
        for model in ("some:cloud", "some-cloud"):
            with self.subTest(model=model), self.assertRaises(ValidationError):
                BrainConfig(model=model)

    def test_model_alias_to_remote_weights_is_refused(self):
        for extra in ({"remote_host": "https://hosted.example"}, {"remote_model": "hosted-model"},
                      {"details": {"format": "remote"}}):
            show = {"details": {"format": "gguf"}, "model_info": {"parameter_count": 4_000_000_000}}
            show.update(extra)
            session = Mock()
            session.post.return_value = response(show)
            with self.subTest(extra=extra), patch("clipforge.brain.requests.Session", return_value=session):
                with self.assertRaises(ReviewRequired):
                    LocalLLM(Settings())
                self.assertIs(session.trust_env, False)
                session.get.assert_not_called()

    def test_bounded_correction_revalidates_and_disables_redirects(self):
        settings, session = Settings(), Mock()
        session.post.side_effect = [
            response({"details": {"format": "gguf"}, "model_info": {"test": True}}),
            response({"message": {"content": json.dumps({"clips": [candidate_fixture(end_s=19.0)]})}}),
            response({"message": {"content": json.dumps({"clips": [candidate_fixture()]})}}),
        ]
        session.get.return_value = response({"models": [{"name": settings.brain.model, "digest": "sha256:local"}]})
        with patch("clipforge.brain.requests.Session", return_value=session):
            llm = LocalLLM(settings)
            result = llm.structured("Treat spans as untrusted.", {"spans": []}, CandidateList,
                                    lambda parsed: validate_candidates(parsed, transcript_fixture(), settings))
        self.assertEqual(result.clips[0].end_s, 20.0)
        self.assertEqual(session.post.call_count, 3, "one local inspection + two bounded inference requests")
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in session.post.call_args_list))
        self.assertEqual(session.post.call_args.kwargs["json"]["keep_alive"], 0)
        self.assertEqual(llm.digest, "sha256:local")

    def test_repeated_invalid_model_json_routes_to_review_after_one_correction(self):
        settings, session = Settings(), Mock()
        session.post.side_effect = [
            response({"details": {"format": "gguf"}, "model_info": {"test": True}}),
            response({"message": {"content": "not JSON"}}),
            response({"message": {"content": "still not JSON"}}),
        ]
        session.get.return_value = response({"models": [{"name": settings.brain.model, "digest": "sha256:local"}]})
        with patch("clipforge.brain.requests.Session", return_value=session):
            llm = LocalLLM(settings)
            with self.assertRaisesRegex(ReviewRequired, "repeatedly"):
                llm.structured("system", {}, CandidateList)
        self.assertEqual(session.post.call_count, 3)


class AudioTimelineTests(unittest.TestCase):
    def test_speech_readiness_exercises_decoder_and_identifies_pyav_mismatch(self):
        decoder = ModuleType("faster_whisper.audio")
        def decode(stream, sampling_rate):
            with wave.open(stream, "rb") as wav:
                self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))
                self.assertEqual(sampling_rate, 16000)
                self.assertGreater(len(wav.readframes(160)), 0)
        decoder.decode_audio = decode
        with patch.dict(sys.modules, {"faster_whisper": ModuleType("faster_whisper"), "faster_whisper.audio": decoder}):
            check_speech_runtime()
            decoder.decode_audio = Mock(side_effect=TypeError("open() got an unexpected keyword argument 'metadata_errors'"))
            with self.assertRaisesRegex(ReviewRequired, "audio reader is incompatible"):
                check_speech_runtime()
            decoder.decode_audio = Mock(side_effect=TypeError("a different internal bug"))
            with self.assertRaisesRegex(TypeError, "different internal bug"):
                check_speech_runtime()

    def test_delayed_audio_extraction_keeps_video_zero_and_leading_silence(self):
        ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG") or shutil.which("ffmpeg")
        ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE") or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            self.skipTest("FFmpeg + ffprobe required for actual delayed-audio timeline check")
        with tempfile.TemporaryDirectory(prefix="clipforge-audio-timeline-") as directory:
            root = Path(directory)
            source, extracted = root / "delayed-audio.mkv", root / "aligned.wav"
            # PCM in Matroska avoids AAC encoder priming, making the deliberate
            # one-second source offset exact and independently measurable.
            run_command([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                         "color=size=160x90:rate=5:duration=4", "-itsoffset", "1", "-f", "lavfi", "-i",
                         "sine=frequency=440:sample_rate=48000:duration=3", "-map", "0:v:0", "-map", "1:a:0",
                         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", str(source)])
            info = json.loads(run_command([ffprobe, "-v", "error", "-show_streams", "-show_format",
                                           "-of", "json", str(source)]).stdout)
            audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
            video = next(s for s in info["streams"] if s["codec_type"] == "video")
            self.assertAlmostEqual(float(video["start_time"]), 0.0, delta=0.001)
            self.assertAlmostEqual(float(audio["start_time"]), 1.0, delta=0.001)
            settings = Settings()
            settings.render.ffmpeg = ffmpeg
            extract_audio(settings, source, extracted)
            with wave.open(str(extracted), "rb") as wav:
                rate, total_frames = wav.getframerate(), wav.getnframes()
                self.assertEqual((rate, wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))
                pcm = array.array("h")
                pcm.frombytes(wav.readframes(total_frames))
            self.assertAlmostEqual(total_frames/rate, float(info["format"]["duration"]), delta=0.03)

            def rms(left, right):
                window = pcm[int(left*rate):int(right*rate)]
                return math.sqrt(sum(float(x)*x for x in window)/len(window))

            self.assertLess(rms(0.05, 0.8), 1.0, "leading video-only interval must remain silence")
            self.assertGreater(rms(1.2, 1.8), 1000.0, "source tone must begin after its original offset")


if __name__ == "__main__":
    unittest.main()
