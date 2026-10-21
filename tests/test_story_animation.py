"""Real offline story encodes and deterministic animation behavior."""
import copy
import json
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from clipforge.common import Settings, run_command
from clipforge.job_control import JobCancelled, job_control
from clipforge.story_animation import BACKDROPS, SETTINGS, available, frame_at, render_story


def project_fixture():
    return {"duration_s": 2.0, "options": {"quality": 720, "fps": 12, "captions": True},
            "characters": [{"id": "boy", "name": "Alex", "color": "#ffc16b", "accessory": "scarf"},
                           {"id": "shade", "name": "Shadow", "color": "#b09acf", "accessory": "none"}],
            "scenes": [{"id": "one", "start_s": 0., "duration_s": 1., "narration": "The window moved.",
                        "setting": "bedroom", "action": "walk", "camera": "pan", "lighting": "normal",
                        "character_ids": ["boy", "shade"], "words": [{"text": "The", "start_s": 0., "end_s": .25},
                        {"text": "window", "start_s": .25, "end_s": .65}, {"text": "moved.", "start_s": .65, "end_s": .98}]},
                       {"id": "two", "start_s": 1., "duration_s": 1., "narration": "Someone watched.",
                        "setting": "hallway", "action": "look", "camera": "zoom", "lighting": "nightvision",
                        "effect": "security", "character_ids": ["boy"], "words": [{"text": "Someone", "start_s": 1.05, "end_s": 1.4},
                        {"text": "watched.", "start_s": 1.4, "end_s": 1.95}]}]}


@unittest.skipUnless(available(), "Install optional Pillow story support")
class StoryFramesTests(unittest.TestCase):
    def test_deterministic_frames_and_articulated_motion(self):
        project = project_fixture()
        first = frame_at(project, .35)
        repeat = frame_at(project, .35)
        later = frame_at(project, .7)
        self.assertEqual(first.tobytes(), repeat.tobytes())
        self.assertNotEqual(first.crop((280, 700, 650, 1160)).tobytes(), later.crop((280, 700, 650, 1160)).tobytes())
        self.assertEqual(first.size, (720, 1280))
        # Drawing frames must not mutate the reusable character library.
        self.assertEqual(project, project_fixture())

    def test_captions_respect_actual_audio_timestamps_and_can_be_disabled(self):
        project = project_fixture()
        with_text = frame_at(project, .4)
        project["options"]["captions"] = False
        without_text = frame_at(project, .4)
        self.assertNotEqual(with_text.crop((30, 140, 690, 300)).tobytes(), without_text.crop((30, 140, 690, 300)).tobytes())
        self.assertEqual(with_text.crop((0, 500, 720, 1280)).tobytes(), without_text.crop((0, 500, 720, 1280)).tobytes())

    def test_phrase_timed_voice_shows_a_wrapped_phrase(self):
        project = project_fixture()
        project["scenes"][0]["words"] = [{"text": "The window opened into a world he had never seen before.",
                                           "start_s": .1, "end_s": .9, "timing": "phrase"}]
        framed = frame_at(project, .4)
        project["options"]["captions"] = False
        plain = frame_at(project, .4)
        self.assertNotEqual(framed.crop((40, 140, 680, 350)).tobytes(), plain.crop((40, 140, 680, 350)).tobytes())

    def test_backgrounds_and_actions_render_without_external_assets(self):
        project = project_fixture()
        for i, setting in enumerate(sorted(SETTINGS)):
            for action in ("walk", "run", "point", "hide", "enter", "exit", "look"):
                with self.subTest(setting=setting, action=action):
                    project["scenes"][0].update(setting=setting, action=action)
                    self.assertEqual(frame_at(project, .45).mode, "RGB")
        project["options"]["quality"] = 1080
        self.assertEqual(frame_at(project, .45, width=1080).size, (1080, 1920))

    def test_window_world_matches_the_storyboard_and_has_subtle_motion(self):
        project = project_fixture()
        project["scenes"][0].update(camera="static", action="idle")
        views = []
        for backdrop in sorted(BACKDROPS):
            with self.subTest(backdrop=backdrop):
                project["scenes"][0]["backdrop"] = backdrop
                first = frame_at(project, .35).crop((460, 460, 645, 755))
                second = frame_at(project, .65).crop((460, 460, 645, 755))
                self.assertNotEqual(first.tobytes(), second.tobytes())
                views.append(first.tobytes())
        self.assertEqual(len(set(views)), len(BACKDROPS))


@unittest.skipUnless(available(), "Install optional Pillow story support")
class StoryEncodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG") or shutil.which("ffmpeg")
        ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE") or shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise unittest.SkipTest("FFmpeg + ffprobe are needed for actual story encodes")
        cls.temp = tempfile.TemporaryDirectory(prefix="clipforge-story-encode-")
        cls.root = Path(cls.temp.name)
        cls.settings = Settings()
        cls.settings.render.ffmpeg, cls.settings.render.ffprobe = ffmpeg, ffprobe
        cls.wav = cls.root / "narration audio.wav"
        with wave.open(str(cls.wav), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000)
            output.writeframes(b"\0\0" * 48000)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_video_is_vertical_h264_aac_and_decodes(self):
        out = self.root / "complete.mp4"
        result = render_story(project_fixture(), self.wav, out, self.settings)
        info = json.loads(run_command([self.settings.render.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)]).stdout)
        videos = [s for s in info["streams"] if s["codec_type"] == "video"]
        audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
        self.assertEqual((videos[0]["width"], videos[0]["height"]), (720, 1280))
        self.assertEqual(videos[0]["codec_name"], "h264")
        self.assertEqual(videos[0]["pix_fmt"], "yuv420p")
        self.assertEqual(audio[0]["codec_name"], "aac")
        self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=.15)
        self.assertEqual(result["duration_s"], 2)
        run_command([self.settings.render.ffmpeg, "-v", "error", "-xerror", "-i", str(out), "-f", "null", "-"])

    def test_cancel_reaps_encoder_and_removes_partial_video(self):
        stopped = False
        def progress(stage, completed, total):
            nonlocal stopped
            if completed and completed >= 6:
                stopped = True
        output = self.root / "cancelled.mp4"
        with self.assertRaises(JobCancelled), job_control(cancelled=lambda: stopped, on_progress=progress):
            render_story(project_fixture(), self.wav, output, self.settings)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob("*.rendering.mp4")))

    def test_existing_video_and_invalid_options_are_not_overwritten(self):
        output = self.root / "keep.mp4"
        output.write_bytes(b"a previously completed video")
        with self.assertRaises(ValueError):
            render_story(project_fixture(), self.wav, output, self.settings)
        self.assertEqual(output.read_bytes(), b"a previously completed video")
        for value in (False, 3840):
            project = copy.deepcopy(project_fixture()); project["options"]["quality"] = value
            with self.assertRaises(ValueError):
                render_story(project, self.wav, self.root / "invalid.mp4", self.settings)
        for value in (float("nan"), -1, 301):
            project = copy.deepcopy(project_fixture()); project["duration_s"] = value
            with self.assertRaises(ValueError):
                render_story(project, self.wav, self.root / "invalid.mp4", self.settings)


if __name__ == "__main__":
    unittest.main()
