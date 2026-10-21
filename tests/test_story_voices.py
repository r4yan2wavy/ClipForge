"""Pinned voice installation integrity, cancellation and real optional narration."""
import array
import hashlib
import json
import os
from pathlib import Path

import pytest

from clipforge.common import Settings, load_settings
from clipforge.job_control import JobCancelled, job_control
from clipforge.story_audio import StoryAudioError, _read_pcm, _write_pcm, synthesize_story
from clipforge import story_voices as voices


class Response:
    def __init__(self, data=b"", status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
    def iter_content(self, chunk_size):
        yield self.data
    def close(self):
        pass


@pytest.fixture
def fixture_pack(tmp_path, monkeypatch):
    cfg = Settings()
    cfg.paths.root = str(tmp_path)
    files = {"MODEL_CARD": b"Fixture attribution", "en_US-fixture-medium.onnx": b"verified local model",
             "en_US-fixture-medium.onnx.json": json.dumps({"num_speakers": 1, "audio": {"sample_rate": 22050}}).encode()}
    pack = {**voices._PACKS[0], "id": "en_US-fixture-medium", "location": "en/en_US/fixture/medium",
            "files": tuple((name, len(data), hashlib.sha256(data).hexdigest()) for name, data in files.items())}
    monkeypatch.setattr(voices, "_PACKS", (pack,))
    monkeypatch.setattr(voices, "_piper_command", lambda _: ["fixture-piper"])
    monkeypatch.setattr(voices, "refresh_voices", lambda _: [])

    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs): return Response(files[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(voices.requests, "Session", Session)
    return cfg, pack, files, Session


def test_verified_install_retains_card_and_license_and_reuses_exact_files(fixture_pack, monkeypatch):
    cfg, pack, files, Session = fixture_pack
    installed = voices.install_voice(pack["id"], cfg)
    assert installed["installed"] and not installed["already_installed"]
    root = Path(cfg.paths.root) / "models/piper"
    assert (root / (pack["id"] + ".MODEL_CARD")).read_bytes() == files["MODEL_CARD"]
    assert json.loads((root / (pack["id"] + ".license.json")).read_text())["revision"] == voices.REVISION
    monkeypatch.setattr(Session, "get", lambda *a, **k: pytest.fail("An intact pack must not redownload"))
    assert voices.install_voice(pack["id"], cfg)["already_installed"]


def test_wrong_digest_never_replaces_existing_voice(fixture_pack, monkeypatch):
    cfg, pack, files, Session = fixture_pack
    root = Path(cfg.paths.root) / "models/piper"
    root.mkdir(parents=True)
    old = root / (pack["id"] + ".onnx")
    old.write_bytes(b"keep this prior voice")
    monkeypatch.setattr(Session, "get", lambda *a, **k: Response(b"X" * len(files["MODEL_CARD"])))
    with pytest.raises(StoryAudioError, match="integrity"):
        voices.install_voice(pack["id"], cfg)
    assert old.read_bytes() == b"keep this prior voice"
    assert not list(root.glob(".install-*"))


def test_cancellation_removes_partial_download_but_preserves_installed_files(fixture_pack):
    cfg, pack, _, _ = fixture_pack
    stopped = False
    def progress(stage, completed, total):
        nonlocal stopped
        stopped = bool(completed)
    with job_control(cancelled=lambda: stopped, on_progress=progress), pytest.raises(JobCancelled):
        voices.install_voice(pack["id"], cfg)
    root = Path(cfg.paths.root) / "models/piper"
    assert not list(root.glob("*.onnx"))
    assert not list(root.glob(".install-*"))


def test_unknown_pack_and_unapproved_download_redirect_are_rejected(fixture_pack, monkeypatch):
    cfg, pack, _, Session = fixture_pack
    with pytest.raises(StoryAudioError, match="catalog"):
        voices.install_voice("../../outside", cfg)
    monkeypatch.setattr(Session, "get", lambda *a, **k: Response(status=302, headers={"Location": "http://127.0.0.1/private"}))
    with pytest.raises(StoryAudioError, match="unsupported download address"):
        voices.install_voice(pack["id"], cfg)
    assert not list((Path(cfg.paths.root) / "models/piper").glob("*.onnx"))


def test_voice_install_refuses_redirected_destination_folder(fixture_pack, monkeypatch):
    cfg, pack, _, _ = fixture_pack
    real = voices._redirected
    monkeypatch.setattr(voices, "_redirected", lambda path: path.name == "piper" or real(path))
    with pytest.raises(StoryAudioError, match="regular folder"):
        voices.install_voice(pack["id"], cfg)


def test_preview_uses_fixed_local_text_and_returns_playable_wav(tmp_path, monkeypatch):
    cfg = Settings()
    cfg.paths.root = str(tmp_path)
    monkeypatch.setattr(voices, "discover_voices", lambda _: [{"id": "fixture:voice"}])
    captured = []
    def synthesize(project, folder, settings):
        captured.append(project)
        output = folder / "audio.wav"
        _write_pcm(output, array.array("h", [1, -1]) * 800)
        return {"audio_path": output}
    monkeypatch.setattr(voices, "synthesize_story", synthesize)
    result = voices.preview_voice("fixture:voice", cfg, tmp_path / "preview")
    assert result.name == "preview.wav"
    assert len(_read_pcm(result)) == 1600
    assert captured[0]["scenes"][0]["narration"] == voices.PREVIEW_TEXT
    assert not captured[0]["options"]["music"]


@pytest.mark.skipif(os.environ.get("CLIPFORGE_TEST_PIPER") != "1", reason="Opt-in installed local Piper voice")
def test_real_piper_speed_and_measured_short_phrase_captions(tmp_path):
    cfg = load_settings(Path(os.environ["CLIPFORGE_TEST_CONFIG"]))
    durations = []
    text = "The quiet window opened into another world. A boy reached for his flashlight and slowly turned toward the door."
    for rate in (-3, 0, 3):
        result = synthesize_story({"options": {"voice": "piper:en_US-ljspeech-medium.onnx", "music": False,
                    "effects": False, "duration": 30, "rate": rate},
                    "scenes": [{"id": "one", "narration": text, "setting": "bedroom", "action": "walk"}]},
                    tmp_path / ("rate" + str(rate)), cfg)
        durations.append(result["duration_s"])
        cues = result["scenes"][0]["words"]
        assert result["caption_timing"] == "phrase"
        assert len(cues) >= 3
        assert all(cue["timing"] == "phrase" and len(cue["text"].split()) <= 8 for cue in cues)
        assert all(0 <= cue["start_s"] < cue["end_s"] <= result["duration_s"] + .001 for cue in cues)
        assert " ".join(cue["text"] for cue in cues) == text
    assert durations[0] > durations[1] > durations[2]
