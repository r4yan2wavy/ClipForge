"""Real, local FFmpeg tests. No credentials or AI models are required.

Set CLIPFORGE_TEST_FFMPEG / CLIPFORGE_TEST_FFPROBE for portable executables.
The integration tests skip explicitly when those executables are unavailable.
"""
import array
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipforge.common import Candidate, Settings, SourceManifest, Span, Transcript, Word, run_command
from clipforge.factory import _canonical_interval, probe_media, render_clip


def fixture_transcript():
    return Transcript(language="en", duration_s=22.0,
                      words=[Word(start_s=0.4+i, end_s=0.9+i, text=f"Word {i}.") for i in range(20)],
                      spans=[Span(id="s0", start_s=0.4, end_s=10.4, text="A clear hook."),
                             Span(id="s1", start_s=10.4, end_s=20.4, text="A useful payoff.")])


def fixture_candidate():
    return Candidate(start_id="s0", end_id="s1", start_s=0.4, end_s=20.4,
                     hook_id="s0", payoff_id="s1", score=0.8,
                     reason="Synthetic mechanics fixture.", needs_visual_review=False)


class RenderBoundaryTests(unittest.TestCase):
    def test_final_boundaries_are_canonical(self):
        self.assertEqual(_canonical_interval(fixture_transcript(), fixture_candidate()), (0.4, 20.4))
        wrong = fixture_candidate().model_copy(update={"start_s": 0.40001})
        with self.assertRaisesRegex(ValueError, "exactly match"):
            _canonical_interval(fixture_transcript(), wrong)
        invented = fixture_candidate().model_copy(update={"end_id": "invented"})
        with self.assertRaisesRegex(ValueError, "unknown"):
            _canonical_interval(fixture_transcript(), invented)


class FFmpegRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG") or shutil.which("ffmpeg")
        cls.ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE") or shutil.which("ffprobe")
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest("FFmpeg + ffprobe are required for actual render checks")
        cls.temp = tempfile.TemporaryDirectory(prefix="clipforge-render-")
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "source [safe]; apostrophe's.mp4"
        cls.music = cls.root / "licensed short loop.wav"
        run_command([cls.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                     "testsrc2=size=320x180:rate=5:duration=22", "-f", "lavfi", "-i",
                     "sine=frequency=440:sample_rate=48000:duration=22", "-shortest", "-c:v",
                     "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                     str(cls.source)])
        # Half a second is intentionally much shorter than the 20 second clip.
        run_command([cls.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                     "sine=frequency=880:sample_rate=48000:duration=0.5", str(cls.music)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def settings(self, *, full_size=False):
        settings = Settings()
        settings.paths.root = str(self.root)
        settings.render.ffmpeg, settings.render.ffprobe = self.ffmpeg, self.ffprobe
        settings.render.preset, settings.render.fps = "ultrafast", 5
        if not full_size:
            settings.render.width, settings.render.height = 180, 320
            settings.render.font_size, settings.render.margin_v = 12, 32
        return settings

    def manifest(self, **kwargs):
        return SourceManifest(media_path=str(self.source), rights_category="original",
                              permission_reference="Synthetic FFmpeg fixture, generated locally.",
                              creator_credit="Synthetic fixture", contribution="Testing video assembly.", **kwargs)

    def test_default_dimensions_decode_and_repeat_run_reuses_file(self):
        settings = self.settings(full_size=True)
        output = self.root / "crop output [safe]; apostrophe's"
        first = render_clip(settings, self.source, fixture_transcript(), fixture_candidate(),
                            self.manifest(), output)
        video = Path(first["video_path"])
        info = probe_media(settings, video)
        stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((stream["width"], stream["height"]), (1080, 1920))
        self.assertAlmostEqual(float(info["format"]["duration"]), 20.0, delta=0.21)
        original_mtime = video.stat().st_mtime_ns
        with patch("clipforge.factory.verify_output", side_effect=AssertionError("should reuse")):
            second = render_clip(settings, self.source, fixture_transcript(), fixture_candidate(),
                                 self.manifest(), output)
        self.assertEqual(first["render_sha256"], second["render_sha256"])
        self.assertEqual(video.stat().st_mtime_ns, original_mtime)
        self.assertTrue(Path(first["caption_srt_path"]).is_file())
        self.assertTrue(Path(first["caption_ass_path"]).is_file())

    def test_fit_and_blurred_background_are_decodable(self):
        for framing in ("fit", "blur"):
            with self.subTest(framing=framing):
                result = render_clip(self.settings(), self.source, fixture_transcript(), fixture_candidate(),
                                     self.manifest(framing=framing), self.root / framing)
                self.assertTrue(Path(result["video_path"]).stat().st_size > 1000)

    def test_floating_point_subtraction_does_not_reject_exact_duration_boundary(self):
        settings = self.settings()
        settings.brain.min_clip_s = 10.0
        candidate = fixture_candidate().model_copy(update={"start_id": "s1", "start_s": 10.4,
                                                            "hook_id": "s1"})
        self.assertLess(candidate.end_s-candidate.start_s, 10.0)
        result = render_clip(settings, self.source, fixture_transcript(), candidate,
                             self.manifest(), self.root / "duration-boundary")
        self.assertTrue(Path(result["video_path"]).is_file())

    def test_delayed_source_audio_keeps_clip_relative_silence_and_caption_alignment(self):
        delayed = self.root / "delayed-audio.mkv"
        run_command([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                     "color=size=160x90:rate=5:duration=4", "-itsoffset", "1", "-f", "lavfi", "-i",
                     "sine=frequency=440:sample_rate=48000:duration=3", "-map", "0:v:0", "-map", "1:a:0",
                     "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", str(delayed)])
        settings = self.settings()
        settings.brain.min_clip_s = 2.0
        transcript = Transcript(language="en", duration_s=4.0,
                                words=[Word(start_s=1.0, end_s=1.4, text="Tone begins."),
                                       Word(start_s=3.0, end_s=3.4, text="Tone continues.")],
                                spans=[Span(id="s0", start_s=0.4, end_s=1.4, text="Tone begins."),
                                       Span(id="s1", start_s=1.4, end_s=3.4, text="Tone continues.")])
        candidate = fixture_candidate().model_copy(update={"end_s": 3.4})
        result = render_clip(settings, delayed, transcript, candidate, self.manifest(),
                             self.root / "delayed-audio-render")
        pcm = subprocess.check_output([self.ffmpeg, "-v", "error", "-i", result["video_path"],
                                       "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"])
        samples = array.array("f")
        samples.frombytes(pcm)

        def rms(left, right):
            segment = samples[int(left*8000):int(right*8000)]
            return math.sqrt(sum(float(x)*x for x in segment)/len(segment))

        self.assertLess(rms(0.05, 0.45), 0.001, "source audio offset must remain a silent gap after the cut")
        self.assertGreater(rms(0.8, 1.2), 0.01, "source tone must remain audible after its preserved offset")
        captions = Path(result["caption_srt_path"]).read_text(encoding="utf-8")
        self.assertIn("00:00:00,600 --> 00:00:01,000", captions,
                      "source word at1.0 must share audio's0.6 clip-relative start")

    def test_short_music_loops_and_mixes_with_speech_at_ten_seconds(self):
        settings = self.settings()
        settings.render.music_gain_db = -12.0
        result = render_clip(settings, self.source, fixture_transcript(), fixture_candidate(),
                             self.manifest(music_path=str(self.music),
                                           music_license_reference="Generated synthetic sine; original.",
                                           music_attribution="Synthetic 880 Hz fixture"), self.root / "music")
        # The entire final render was decoded by render_clip. Inspect a late audio
        # window to demonstrate both voices after the source music file has ended.
        pcm = subprocess.check_output([self.ffmpeg, "-v", "error", "-ss", "10", "-t", "1",
                                       "-i", result["video_path"], "-vn", "-ac", "1", "-ar", "8000",
                                       "-f", "f32le", "-"])
        samples = array.array("f")
        samples.frombytes(pcm)

        def magnitude(frequency):
            n = len(samples)
            real = sum(x * math.cos(2*math.pi*frequency*i/8000) for i, x in enumerate(samples))
            imag = sum(x * math.sin(2*math.pi*frequency*i/8000) for i, x in enumerate(samples))
            return 2 * math.hypot(real, imag) / n

        self.assertGreater(magnitude(440), 0.01, "source speech fixture must remain audible")
        self.assertGreater(magnitude(880), 0.0001, "music must loop and remain in the late mix")
        self.assertEqual(result["render_spec"]["music_attribution"], "Synthetic 880 Hz fixture")

    def test_missing_source_audio_is_routed_to_review(self):
        silent = self.root / "no-audio.mp4"
        run_command([self.ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(self.source),
                     "-an", "-c:v", "copy", str(silent)])
        with self.assertRaisesRegex(ValueError, "source has no audio"):
            render_clip(self.settings(), silent, fixture_transcript(), fixture_candidate(),
                        self.manifest(), self.root / "missing-audio")


if __name__ == "__main__":
    unittest.main()
