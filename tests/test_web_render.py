"""Real FFmpeg checks for the local studio's independent manual editing path."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipforge.common import Settings, run_command
from clipforge.factory import probe_media
from clipforge.web_render import render_manual


class ManualRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG") or shutil.which("ffmpeg")
        ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE") or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise unittest.SkipTest("FFmpeg + ffprobe are needed for actual video checks")
        cls.temp = tempfile.TemporaryDirectory(prefix="clipforge-manual-")
        cls.root = Path(cls.temp.name)
        cls.settings = Settings()
        cls.settings.render.ffmpeg, cls.settings.render.ffprobe = ffmpeg, ffprobe
        cls.settings.render.width, cls.settings.render.height = 180, 320
        cls.settings.render.fps, cls.settings.render.preset = 5, "ultrafast"
        cls.settings.render.font_size, cls.settings.render.margin_v = 12, 32
        cls.silent = cls.root / "silent source's [test].mp4"
        cls.audio = cls.root / "short audio.mp4"
        run_command([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                     "testsrc2=size=320x180:rate=5:duration=4", "-c:v", "libx264",
                     "-preset", "ultrafast", str(cls.silent)])
        run_command([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(cls.silent),
                     "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
                     "-c:v", "copy", "-c:a", "aac", str(cls.audio)])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_silent_video_real_cut_decode_and_repeat_cache_without_ai(self):
        stages = []
        result = render_manual(self.settings, self.silent, self.root / "silent-output",
            start_s=0.4, end_s=2.4, framing="center_crop", on_stage=stages.append)
        info = probe_media(self.settings, Path(result["video_path"]))
        self.assertEqual({s["codec_type"] for s in info["streams"]}, {"audio", "video"})
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.15)
        self.assertIsNone(result["caption_path"])
        self.assertEqual(stages, ["preparing", "rendering", "verifying", "ready"])
        self.assertNotIn("transcript", result)
        with patch("clipforge.web_render.verify_output", side_effect=AssertionError("must reuse")):
            second = render_manual(self.settings, self.silent, self.root / "silent-output",
                start_s=0.4, end_s=2.4, framing="center_crop")
        self.assertEqual(result["render_sha256"], second["render_sha256"])

    def test_manual_overlay_is_escaped_and_not_a_speech_transcript(self):
        result = render_manual(self.settings, self.audio, self.root / "text-output",
            start_s=0.2, end_s=3.2, framing="blur", caption_text=r"{\pos(0,0)} <b>My caption</b>")
        ass = (self.root / "text-output" / "captions.ass").read_text(encoding="utf-8")
        self.assertIn("｛＼pos(0,0)｝", ass)
        self.assertNotIn(r"{\pos(0,0)}", ass)
        self.assertEqual(result["render_spec"]["caption_kind"], "manual_overlay")
        self.assertEqual(result["duration_s"], 3.0)
        self.assertTrue(Path(result["caption_path"]).is_file())

    def test_bounds_reject_nan_booleans_overshoot_and_too_long(self):
        for start, end in [(False, 2), (float("nan"), 2), (-1, 2), (3, 2), (0, 5), (0, 181)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                render_manual(self.settings, self.silent, self.root / "invalid",
                    start_s=start, end_s=end, framing="fit")

    def test_caption_can_be_removed_on_rerender(self):
        folder = self.root / "remove-text"
        render_manual(self.settings, self.silent, folder, start_s=0, end_s=1,
                      framing="fit", caption_text="Manual text")
        result = render_manual(self.settings, self.silent, folder, start_s=0, end_s=1, framing="fit")
        self.assertIsNone(result["caption_path"])
        self.assertFalse((folder / "captions.ass").exists())


if __name__ == "__main__":
    unittest.main()
