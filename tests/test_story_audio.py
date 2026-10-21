"""Measured local speech timing and offline soundtrack production contracts."""
from __future__ import annotations

import array
import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from clipforge.common import Settings
from clipforge.job_control import JobCancelled, job_control
from clipforge import story_audio as audio


def settings(tmp_path):
    cfg = Settings()
    cfg.paths.root = str(tmp_path)
    return cfg


def voice():
    return {"id": "sapi:Test Voice", "name": "Test Voice", "provider": "Windows offline", "language": "en-US"}


def make_pcm(duration, frequency=220):
    return array.array("h", [round(math.sin(i / audio.SAMPLE_RATE * math.tau * frequency) * 8000)
                             for i in range(round(duration * audio.SAMPLE_RATE))])


def test_word_timestamps_keep_real_speech_positions_and_absolute_scene_offset():
    marks = [{"text": "back", "start_s": .62}, {"text": "look", "start_s": .1},
             {"text": "ignored", "start_s": float("nan")}, {"text": "outside", "start_s": 8}]
    words, mode = audio._align_words(marks, "look back", .9, 4)
    assert mode == "word"
    assert words == [{"text": "look", "start_s": 4.1, "end_s": 4.62, "timing": "word"},
                     {"text": "back", "start_s": 4.62, "end_s": 4.9, "timing": "word"}]


def test_missing_provider_word_marks_use_whole_measured_phrase_not_invented_word_times():
    words, mode = audio._align_words([], "A shadow moved behind her.", 3.7, 8)
    assert mode == "phrase"
    assert words == [{"text": "A shadow moved behind her.", "start_s": 8, "end_s": 11.7, "timing": "phrase"}]


def test_sapi_inputs_are_json_data_and_never_embedded_in_executable_code(tmp_path, monkeypatch):
    text = "A door whispered: $(Remove-Item anything); 'quoted' <speak> literally."
    received = []

    def command(args, **kwargs):
        received.append(args)
        payload = json.loads(Path(args[-2]).read_text(encoding="utf-8"))
        assert payload["scenes"][0]["text"] == text
        helper = Path(args[-3]).read_text(encoding="utf-8-sig")
        assert text not in helper
        assert "speaker.Speak(text)" in helper
        Path(args[-1]).write_text(json.dumps([{"words": [{"text": "A", "start_s": .1}]}]), encoding="utf-8")
        kwargs["on_stdout"]("scene=1")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(audio, "run_command", command)
    marks = audio._sapi_scenes("Test Voice", [{"narration": text}], tmp_path, 0)
    assert marks == [[{"text": "A", "start_s": .1}]]
    assert received[0][:4] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]


def test_audio_pipeline_uses_wav_length_for_scene_and_caption_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "discover_voices", lambda _: [voice()])

    def narrate(_voice, scenes, folder, rate):
        assert rate == 0
        audio._write_pcm(folder / "scene-000.wav", make_pcm(.4))
        audio._write_pcm(folder / "scene-001.wav", make_pcm(.6))
        return [[{"text": "Hello", "start_s": .1}], [{"text": "World", "start_s": .15}]]

    monkeypatch.setattr(audio, "_sapi_scenes", narrate)
    project = {"options": {"music": False, "effects": False, "duration": 30},
               "scenes": [{"id": "one", "narration": "Hello"}, {"id": "two", "narration": "World"}]}
    result = audio.synthesize_story(project, tmp_path / "attempt", settings(tmp_path))
    assert result["duration_s"] == pytest.approx(1.18)
    assert result["scenes"][0]["duration_s"] == pytest.approx(.58)
    assert result["scenes"][1]["start_s"] == pytest.approx(.58)
    assert result["scenes"][1]["words"][0]["start_s"] == pytest.approx(.73)
    assert len(audio._read_pcm(result["audio_path"])) == round(1.18 * audio.SAMPLE_RATE)
    assert result["caption_timing"] == "word"
    assert "start_s" not in project["scenes"][0]  # Input document is not mutated.
    assert (tmp_path / "attempt/narration-timeline.json").is_file()
    assert "never cut off" in result["warnings"][0]


def test_original_ambient_score_is_deterministic_and_peak_limited():
    pcm = make_pcm(.35)
    scenes = [{"start_s": 0, "duration_s": .35, "action": "walk", "effect": "shake",
               "words": [{"start_s": .05, "end_s": .3}]}]
    dry = audio._mix_ambient(pcm, scenes, False, False)
    wet = audio._mix_ambient(pcm, scenes, True, True)
    assert wet == audio._mix_ambient(pcm, scenes, True, True)
    assert len(dry) == len(wet) == len(pcm)
    assert wet != dry
    assert max(abs(x) for x in wet) < 32768


def test_cancellation_during_soundtrack_preserves_original_project_and_no_final_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "discover_voices", lambda _: [voice()])

    def narrate(_voice, scenes, folder, rate):
        audio._write_pcm(folder / "scene-000.wav", make_pcm(2))
        return [[{"text": "Hello", "start_s": .1}]]

    monkeypatch.setattr(audio, "_sapi_scenes", narrate)
    cancelled = False

    def progress(stage, completed, total):
        nonlocal cancelled
        if stage == "story_audio" and completed and completed > 0:
            cancelled = True

    with pytest.raises(JobCancelled):
        with job_control(cancelled=lambda: cancelled, on_progress=progress):
            audio.synthesize_story({"options": {}, "scenes": [{"narration": "Hello"}]}, tmp_path / "attempt", settings(tmp_path))
    assert not (tmp_path / "attempt/story-audio.wav").exists()


def test_missing_voice_and_removed_voice_are_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "discover_voices", lambda _: [])
    project = {"options": {}, "scenes": [{"narration": "Hello"}]}
    with pytest.raises(audio.StoryAudioError, match="No offline narrator"):
        audio.synthesize_story(project, tmp_path / "one", settings(tmp_path))
    monkeypatch.setattr(audio, "discover_voices", lambda _: [voice()])
    project["options"]["voice"] = "sapi:Removed"
    with pytest.raises(audio.StoryAudioError, match="no longer installed"):
        audio.synthesize_story(project, tmp_path / "two", settings(tmp_path))


def test_empty_scene_and_unsupported_speed_are_rejected_before_process_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "discover_voices", lambda _: [voice()])
    with pytest.raises(audio.StoryAudioError, match="between 1 and 32"):
        audio.synthesize_story({"scenes": []}, tmp_path / "one", settings(tmp_path))
    with pytest.raises(audio.StoryAudioError, match="narration"):
        audio.synthesize_story({"scenes": [{"narration": ""}]}, tmp_path / "two", settings(tmp_path))
    with pytest.raises(audio.StoryAudioError, match="speed"):
        audio.synthesize_story({"options": {"rate": True}, "scenes": [{"narration": "Hello"}]}, tmp_path / "three", settings(tmp_path))


def test_unreadable_or_silent_local_voice_file_is_not_reported_as_success(tmp_path):
    with pytest.raises(audio.StoryAudioError, match="complete audio"):
        audio._read_pcm(tmp_path / "missing.wav")
    audio._write_pcm(tmp_path / "silent.wav", array.array("h", [0]) * 160)
    with pytest.raises(audio.StoryAudioError, match="silent"):
        audio._read_pcm(tmp_path / "silent.wav")


def test_voice_discovery_cache_is_copied_to_avoid_ui_mutation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(audio, "_find_voices", lambda _: calls.append(1) or [voice()])
    cfg = settings(tmp_path)
    first = audio.discover_voices(cfg)
    first[0]["name"] = "Changed in browser"
    assert audio.discover_voices(cfg)[0]["name"] == "Test Voice"
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt" or os.environ.get("CLIPFORGE_TEST_SAPI") != "1",
                    reason="Opt-in real Windows desktop voice synthesis")
def test_real_windows_voice_produces_measured_words_and_playable_wav(tmp_path):
    cfg = settings(tmp_path)
    voices = audio.discover_voices(cfg)
    sapi = next(v for v in voices if v["id"].startswith("sapi:"))
    result = audio.synthesize_story({"options": {"voice": sapi["id"], "music": True, "effects": True, "duration": 30},
                                    "scenes": [{"id": "bedroom", "narration": "The window opened quietly. A distant shadow moved.", "setting": "bedroom", "action": "look"}]},
                                   tmp_path / "attempt", cfg)
    assert 2 < result["duration_s"] < 12
    assert result["caption_timing"] == "word"
    words = result["scenes"][0]["words"]
    assert len(words) >= 7
    assert [w["start_s"] for w in words] == sorted(w["start_s"] for w in words)
    assert all(0 <= w["start_s"] < w["end_s"] <= result["duration_s"] for w in words)
    assert len(audio._read_pcm(result["audio_path"])) == round(result["duration_s"] * audio.SAMPLE_RATE)
