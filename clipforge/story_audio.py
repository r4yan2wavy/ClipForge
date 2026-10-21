"""Offline story narration, measured speech timing, and original ambient audio.

Windows' installed desktop voices expose word positions in the actual WAV via
SpeakProgress. Piper/eSpeak installations work too; their subtitles intentionally
use complete spoken phrases rather than pretending to have word alignment.
Nothing in this module downloads models or sends narration to a service.
"""
from __future__ import annotations

import array
import copy
import importlib.util
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from .common import atomic_json, run_command
from .job_control import checkpoint, report

SAMPLE_RATE = 16000
_VOICE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_VOICE_LOCK = threading.Lock()


class StoryAudioError(ValueError):
    """An actionable local narration setup or synthesis error."""


_VOICE_DISCOVERY = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
$speaker=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voices=@($speaker.GetInstalledVoices() | Where-Object Enabled | ForEach-Object {
    @{name=$_.VoiceInfo.Name;language=$_.VoiceInfo.Culture.Name}
  })
  ConvertTo-Json -InputObject $voices -Compress
} finally { $speaker.Dispose() }
"""

# User narration is read from JSON, never inserted into executable PowerShell,
# C#, SSML, shell commands or executable arguments. Speak() treats it as text.
_SAPI_HELPER = r"""
param([string]$PayloadPath,[string]$ResultPath)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Speech
Add-Type -ReferencedAssemblies System.Speech -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Speech.Synthesis;
using System.Speech.AudioFormat;
public class ClipForgeStoryMark {
  public string text;
  public double start_s;
  public int position;
  public int length;
}
public static class ClipForgeStorySpeech {
  public static ClipForgeStoryMark[] Speak(string voice, string text, string file, int rate) {
    var marks = new List<ClipForgeStoryMark>();
    using (var speaker = new SpeechSynthesizer()) {
      speaker.SelectVoice(voice);
      speaker.Rate=rate;
      speaker.Volume=100;
      speaker.SetOutputToWaveFile(file, new SpeechAudioFormatInfo(16000,
        AudioBitsPerSample.Sixteen, AudioChannel.Mono));
      speaker.SpeakProgress += delegate(object sender, SpeakProgressEventArgs e) {
        lock(marks) {
          marks.Add(new ClipForgeStoryMark {text=e.Text, start_s=e.AudioPosition.TotalSeconds,
            position=e.CharacterPosition, length=e.CharacterCount});
        }
      };
      speaker.Speak(text);
      speaker.SetOutputToNull();
    }
    lock(marks) { return marks.ToArray(); }
  }
}
'@
$payload=Get-Content -LiteralPath $PayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
$result=@()
$index=0
foreach($scene in $payload.scenes) {
  $marks=[ClipForgeStorySpeech]::Speak([string]$payload.voice,[string]$scene.text,
    [string]$scene.path,[int]$payload.rate)
  $result+=@{words=@($marks)}
  $index++
  Write-Output ('scene='+$index)
}
$json=ConvertTo-Json -InputObject $result -Depth 8 -Compress
[IO.File]::WriteAllText($ResultPath,$json,[Text.UTF8Encoding]::new($false))
"""


def _piper_executable(settings) -> str | None:
    installed = shutil.which("piper")
    if installed:
        return installed
    for name in ("tools/piper/piper.exe", "tools/piper/piper", "piper/piper.exe"):
        candidate = settings.resolve(name)
        if candidate.is_file():
            return str(candidate)
    return None


def _piper_command(settings) -> list[str] | None:
    """Use this environment's optional Piper package before global executables."""
    try:
        if importlib.util.find_spec("piper") is not None:
            return [sys.executable, "-m", "piper"]
    except (ImportError, ValueError):
        pass
    executable = _piper_executable(settings)
    return [executable] if executable else None


def _find_voices(settings) -> list[dict]:
    voices = []
    if os.name == "nt" and shutil.which("powershell.exe"):
        try:
            output = run_command(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                                  "-Command", _VOICE_DISCOVERY], timeout=20).stdout
            data = json.loads(output.lstrip("\ufeff").strip())
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    voices.append({"id": "sapi:" + item["name"], "name": item["name"],
                                   "provider": "Windows offline", "language": item.get("language", "")})
        except (OSError, ValueError, subprocess.SubprocessError):
            # Missing Windows speech components must leave the studio usable.
            pass
    if _piper_command(settings):
        directory = settings.resolve("models/piper")
        if directory.is_dir():
            for model in sorted(directory.glob("*.onnx")):
                config = model.with_suffix(".onnx.json")
                if (model.is_symlink() or config.is_symlink() or not config.is_file()
                        or config.stat().st_size > 65536 or model.stat().st_size > 512 * 1024 * 1024):
                    continue
                try:
                    meta = json.loads(config.read_text(encoding="utf-8"))
                    language = meta.get("language", {}).get("code", model.stem.split("-")[0])
                except (ValueError, OSError, AttributeError):
                    continue
                voices.append({"id": "piper:" + model.name, "name": model.stem,
                               "provider": "Piper offline", "language": language})
    executable = shutil.which("espeak-ng") or shutil.which("espeak")
    if executable:
        try:
            output = run_command([executable, "--voices=en"], timeout=10).stdout
            for line in output.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 4 and re.fullmatch(r"[a-zA-Z0-9_-]{2,30}", fields[1]):
                    identifier = "espeak:" + fields[1]
                    if not any(v["id"] == identifier for v in voices):
                        voices.append({"id": identifier, "name": fields[3].replace("_", " "),
                                       "provider": "eSpeak offline", "language": fields[1]})
        except (OSError, subprocess.SubprocessError):
            pass
    return voices


def discover_voices(settings) -> list[dict]:
    """Return installed local voices; model paths and credentials stay private."""
    key = str(settings.resolve(".")).lower()
    with _VOICE_LOCK:
        saved = _VOICE_CACHE.get(key)
        if saved and time.monotonic() - saved[0] < 60:
            return copy.deepcopy(saved[1])
    voices = _find_voices(settings)
    with _VOICE_LOCK:
        _VOICE_CACHE[key] = (time.monotonic(), voices)
    return copy.deepcopy(voices)


def refresh_voices(settings) -> list[dict]:
    """Rescan after a deliberate model install or Windows voice change."""
    key = str(settings.resolve(".")).lower()
    with _VOICE_LOCK:
        _VOICE_CACHE.pop(key, None)
    return discover_voices(settings)


def _read_pcm(path: Path) -> array.array:
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise StoryAudioError("The local narrator did not produce readable mono audio.")
            samples = array.array("h", wav.readframes(wav.getnframes()))
    except (OSError, EOFError, wave.Error) as error:
        raise StoryAudioError("The local narrator did not produce a complete audio file. Try another installed voice.") from error
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples or not any(samples):
        raise StoryAudioError("The selected voice produced silent audio. Try another installed local voice.")
    return samples


def _write_pcm(path: Path, samples):
    pcm = array.array("h", samples)
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def _sapi_scenes(voice: str, scenes: list[dict], folder: Path, rate: int) -> list[list[dict]]:
    helper = folder / "speak-local.ps1"
    payload, result = folder / "speech-input.json", folder / "speech-timing.json"
    helper.write_text(_SAPI_HELPER, encoding="utf-8-sig")
    atomic_json(payload, {"voice": voice, "rate": rate, "scenes": [
        {"text": s["narration"], "path": str((folder / f"scene-{i:03d}.wav").resolve())}
        for i, s in enumerate(scenes)]})

    def progress(line):
        if line.startswith("scene="):
            try:
                report("story_narrating", int(line[6:]), len(scenes))
            except ValueError:
                pass

    try:
        run_command(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                     "-ExecutionPolicy", "Bypass", "-File", str(helper),
                     str(payload), str(result)], on_stdout=progress, timeout=480)
        data = json.loads(result.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list) or len(data) != len(scenes):
            raise ValueError("incomplete speech timing")
        return [item.get("words", []) for item in data]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise StoryAudioError("Windows could not narrate the story. Check that the chosen desktop voice is installed, then try again.") from error


def _external_scenes(provider: str, voice: str, scenes: list[dict], folder: Path, settings, rate: int):
    if provider == "piper":
        return _piper_scenes(voice, scenes, folder, settings, rate)
    for index, scene in enumerate(scenes):
        checkpoint()
        target = folder / f"scene-{index:03d}.wav"
        raw = folder / f"scene-{index:03d}-raw.wav"
        executable = shutil.which("espeak-ng") or shutil.which("espeak")
        if not executable:
            raise StoryAudioError("eSpeak is no longer installed. Select an available local voice.")
        command = [executable, "-v", voice, "-s", str(150 + rate * 15), "-w", str(raw), "--stdin"]
        try:
            run_command(command, input=scene["narration"] + "\n", timeout=120)
            run_command([settings.render.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                         "-i", str(raw), "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(target)], timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            raise StoryAudioError(f"The local {provider} narrator could not create audio. Check its installed model and try again.") from error
        raw.unlink(missing_ok=True)
        report("story_narrating", index + 1, len(scenes))
    return [[] for _ in scenes]


def _spoken_phrases(text: str) -> list[str]:
    """Short readable cues, each synthesized and measured as its own phrase."""
    result = []
    for sentence in re.split(r"(?<=[.!?;:])\s+", " ".join(text.split())):
        words = sentence.split()
        while words:
            result.append(" ".join(words[:8]))
            words = words[8:]
    return result


def _piper_scenes(voice: str, scenes: list[dict], folder: Path, settings, rate: int):
    command_prefix = _piper_command(settings)
    model = settings.resolve("models/piper") / voice
    if not command_prefix or not model.is_file() or model.is_symlink():
        raise StoryAudioError("The selected Piper executable or local model is no longer installed.")
    chunks = [(index, phrase) for index, scene in enumerate(scenes)
              for phrase in _spoken_phrases(scene["narration"])]
    raw_folder = folder / "piper-phrases"
    raw_folder.mkdir()
    length_scale = 1.0 / (1.0 + .12 * rate)
    # Piper's official CLI writes one WAV per input line using monotonic
    # timestamp names, loading the model only once for the complete story.
    command = command_prefix + ["-m", str(model), "-d", str(raw_folder),
                                "--length_scale", f"{length_scale:.6f}"]
    try:
        run_command(command, input="\n".join(phrase for _, phrase in chunks) + "\n", timeout=480)
        files = sorted(raw_folder.glob("*.wav"), key=lambda p: int(p.stem) if p.stem.isdigit() else p.stem)
        if len(files) != len(chunks):
            raise StoryAudioError("Piper did not return every narration phrase. Check the selected local voice and retry.")
        marks, samples = [[] for _ in scenes], [array.array("h") for _ in scenes]
        for index, (raw, (scene_index, phrase)) in enumerate(zip(files, chunks)):
            checkpoint()
            normalized = folder / f"phrase-{index:03d}.wav"
            run_command([settings.render.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                         "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(normalized)], timeout=60)
            pcm = _read_pcm(normalized)
            start = len(samples[scene_index]) / SAMPLE_RATE
            samples[scene_index].extend(pcm)
            marks[scene_index].append({"text": phrase, "start_s": start,
                "end_s": len(samples[scene_index]) / SAMPLE_RATE, "timing": "phrase"})
            normalized.unlink(missing_ok=True)
            report("story_narrating", index + 1, len(chunks))
        for index, pcm in enumerate(samples):
            _write_pcm(folder / f"scene-{index:03d}.wav", pcm)
        return marks
    except (OSError, subprocess.SubprocessError) as error:
        raise StoryAudioError("The local Piper narrator could not create audio. Check its installed model and try again.") from error


def _align_words(raw: list[dict], narration: str, duration: float, offset: float) -> tuple[list[dict], str]:
    if raw and all(isinstance(mark, dict) and mark.get("timing") == "phrase" for mark in raw):
        phrases = []
        for mark in raw:
            try:
                start, end = float(mark["start_s"]), float(mark["end_s"])
                if math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration + .001 and str(mark.get("text", "")).strip():
                    phrases.append({"text": str(mark["text"]).strip(), "start_s": offset + start,
                                    "end_s": offset + min(duration, end), "timing": "phrase"})
            except (KeyError, TypeError, ValueError):
                continue
        if phrases:
            return sorted(phrases, key=lambda p: p["start_s"]), "phrase"
    valid = []
    for mark in raw:
        try:
            start = float(mark.get("start_s", -1))
            text = str(mark.get("text", "")).strip()
            if text and math.isfinite(start) and 0 <= start < duration:
                valid.append({"text": text, "start_s": start})
        except (ValueError, TypeError, AttributeError):
            continue
    valid.sort(key=lambda mark: mark["start_s"])
    if not valid:
        # Actual scene audio boundaries, not synthetic evenly-spaced word times.
        return [{"text": narration, "start_s": offset, "end_s": offset + duration,
                 "timing": "phrase"}], "phrase"
    words = []
    for i, mark in enumerate(valid):
        end = valid[i + 1]["start_s"] if i + 1 < len(valid) else duration
        if end > mark["start_s"]:
            words.append({"text": mark["text"], "start_s": offset + mark["start_s"],
                          "end_s": offset + min(duration, end), "timing": "word"})
    if not words:
        return [{"text": narration, "start_s": offset, "end_s": offset + duration,
                 "timing": "phrase"}], "phrase"
    return words, "word"


def _mix_ambient(samples: array.array, scenes: list[dict], music: bool, effects: bool = True) -> array.array:
    """Original synthesized score/effects: no downloaded or licensed recordings."""
    total = len(samples)
    mixed = array.array("h")
    # Small chunks keep cancellation responsive, without a heavyweight DSP tool.
    rng = random.Random(6294)
    scene_index = 0
    for base in range(0, total, SAMPLE_RATE):
        checkpoint()
        end = min(total, base + SAMPLE_RATE)
        for index in range(base, end):
            t = index / SAMPLE_RATE
            while scene_index + 1 < len(scenes) and t >= scenes[scene_index + 1]["start_s"]:
                scene_index += 1
            scene = scenes[scene_index]
            local = t - scene["start_s"]
            # The narrator is active through its measured word/phrase interval.
            words = scene.get("words", [])
            speaking = bool(words and words[0]["start_s"] <= t <= words[-1]["end_s"])
            gain = .010 if speaking else .025
            sound = 0.0
            if music:
                pad = math.sin(math.tau * 110 * t) * .48 + math.sin(math.tau * 164.81 * t) * .24 + math.sin(math.tau * 220.4 * t) * .18
                pulse = .65 + .35 * math.sin(math.tau * .14 * t)
                fade = min(1.0, t / .6, (total / SAMPLE_RATE - t) / .7)
                sound += max(0, fade) * gain * pad * pulse
            if effects:
                # Gentle scene-entry air swells and muted movement taps remain
                # below narration and are derived entirely from original tones.
                if local < .55:
                    envelope = math.sin(math.pi * max(0, local) / .55) ** 2
                    sound += (rng.random() * 2 - 1) * .009 * envelope
                if scene.get("action") in {"walk", "run", "enter", "exit"}:
                    step = local % (.42 if scene.get("action") == "run" else .72)
                    if step < .075:
                        sound += math.sin(math.tau * 78 * step) * math.exp(-step * 65) * .013
                if scene.get("effect") in {"shake", "security"} and local < .3:
                    sound += math.sin(math.tau * 55 * local) * math.exp(-local * 15) * .025
            value = round(samples[index] * .94 + sound * 32767)
            mixed.append(max(-32768, min(32767, value)))
        report("story_audio", end, total)
    return mixed


def synthesize_story(project: dict, folder: Path, settings) -> dict:
    """Produce offline narration plus an original score, with absolute timings.

    ``folder`` is a private per-attempt working directory. Completed project
    revisions are never modified. Cancellation can leave only that attempt's
    temporary files; the caller owns safe disposal of its attempt directory.
    """
    checkpoint()
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    options = project.get("options", {})
    scenes = copy.deepcopy(project.get("scenes", []))
    if not scenes or len(scenes) > 32:
        raise StoryAudioError("Create a storyboard with between 1 and 32 narrated scenes first.")
    if any(not isinstance(s.get("narration"), str) or not s["narration"].strip() or len(s["narration"]) > 3000 for s in scenes):
        raise StoryAudioError("Every scene needs a short narration before it can be rendered.")
    voices = discover_voices(settings)
    if not voices:
        raise StoryAudioError("No offline narrator is available. On Windows, install an English desktop speech voice in Windows Settings. Or install Piper and place a licensed .onnx voice with its .onnx.json file in models/piper, then restart ClipForge.")
    voice_id = options.get("voice") or voices[0]["id"]
    if voice_id == "auto":
        voice_id = voices[0]["id"]
    if voice_id not in {v["id"] for v in voices}:
        raise StoryAudioError("That narrator is no longer installed. Choose an available local voice.")
    rate = options.get("rate", 0)
    if not isinstance(rate, int) or isinstance(rate, bool) or not -3 <= rate <= 3:
        raise StoryAudioError("Narration speed must be a whole number between -3 and 3.")
    provider, voice = voice_id.split(":", 1)
    report("story_narrating", 0, len(scenes))
    raw_marks = (_sapi_scenes(voice, scenes, folder, rate) if provider == "sapi" else
                 _external_scenes(provider, voice, scenes, folder, settings, rate))
    samples = array.array("h")
    silence = array.array("h", [0]) * round(SAMPLE_RATE * .18)
    timing_modes = set()
    for index, scene in enumerate(scenes):
        checkpoint()
        pcm = _read_pcm(folder / f"scene-{index:03d}.wav")
        duration = len(pcm) / SAMPLE_RATE
        start = len(samples) / SAMPLE_RATE
        words, mode = _align_words(raw_marks[index], scene["narration"], duration, start)
        timing_modes.add(mode)
        scene.update(start_s=start, duration_s=duration + (.18 if index < len(scenes) - 1 else 0),
                     words=words, caption_timing=mode)
        samples.extend(pcm)
        if index < len(scenes) - 1:
            samples.extend(silence)
    narration_path = folder / "narration.wav"
    _write_pcm(narration_path, samples)
    duration = len(samples) / SAMPLE_RATE
    if duration > 240:
        raise StoryAudioError("This narration is over four minutes. Shorten the script or choose a faster voice before rendering.")
    report("story_audio", 0, len(samples))
    mixed = _mix_ambient(samples, scenes, bool(options.get("music", True)), bool(options.get("effects", True)))
    output = folder / "story-audio.wav"
    _write_pcm(output, mixed)
    warnings = []
    if "phrase" in timing_modes:
        warnings.append("This voice uses measured spoken-phrase timing for captions rather than individual word highlights.")
    target = options.get("duration", 60)
    if isinstance(target, (int, float)) and target > 0 and abs(duration - target) / target > .25:
        warnings.append(f"The complete narration is {duration:.0f} seconds. The requested duration is a script target; spoken text is never cut off.")
    atomic_json(folder / "narration-timeline.json", {"voice": voice_id, "duration_s": duration, "scenes": scenes,
                                                   "warnings": warnings, "audio_origin": "Original procedural ambient score and effects; local voice narration."})
    checkpoint()
    return {"audio_path": output, "duration_s": duration, "scenes": scenes, "warnings": warnings,
            "voice": voice_id, "caption_timing": "word" if timing_modes == {"word"} else "phrase"}
