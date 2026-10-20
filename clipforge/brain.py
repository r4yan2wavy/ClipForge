from __future__ import annotations

import gc
import io
import json
import re
import wave
from pathlib import Path
from typing import TypeVar

import requests
from pydantic import BaseModel, ValidationError

from .common import (Candidate, CandidateList, Metadata, Settings, Span, Transcript,
                     Word, atomic_json, run_command, sha256_file, stable_id)

PROMPT_VERSION = "1"
PROMPT_DIR = Path(__file__).with_name("prompts")
T = TypeVar("T", bound=BaseModel)


class ReviewRequired(ValueError):
    pass


def check_speech_runtime():
    """Exercise the installed decoder without loading or downloading a model."""
    from faster_whisper.audio import decode_audio
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)
    audio.seek(0)
    try:
        decode_audio(audio, sampling_rate=16000)
    except TypeError as error:
        if "metadata_errors" in str(error):
            raise ReviewRequired("Whisper's audio reader is incompatible. Repair the AI installation and restart ClipForge; see the local AI setup guide.") from error
        raise


def prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


def spans_from_words(words: list[Word]) -> list[Span]:
    """Small canonical boundaries: never alter or compress the original timeline."""
    result, group = [], []
    for word in words:
        group.append(word)
        if (word.end_s - group[0].start_s >= 4 or
                len("".join(w.text for w in group)) >= 200 or
                (len(group) >= 4 and word.text.rstrip().endswith((".", "!", "?")))):
            result.append(Span(id=f"s{len(result):06d}", start_s=group[0].start_s,
                               end_s=group[-1].end_s, text=join_words(group)))
            group = []
    if group:
        result.append(Span(id=f"s{len(result):06d}", start_s=group[0].start_s,
                           end_s=group[-1].end_s, text=join_words(group)))
    return result


def join_words(words: list[Word]) -> str:
    return " ".join(w.text.strip() for w in words).strip()


def load_subtitles(path: Path, duration: float, language: str) -> Transcript:
    """SRT cue times are retained as phrase units, never invented word alignment."""
    data = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    words, spans = [], []
    def seconds(s: str) -> float:
        h, m, rest = s.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    for block in re.split(r"\n\s*\n", data.strip()):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            raise ReviewRequired("malformed SRT cue")
        match = re.fullmatch(r"\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*", lines[timing])
        if not match:
            raise ReviewRequired("SRT timestamps must be standard absolute cue times")
        text = " ".join(lines[timing + 1:]).strip()
        start, end = map(seconds, match.groups())
        words.append(Word(start_s=start, end_s=end, text=text))
        spans.append(Span(id=f"s{len(spans):06d}", start_s=start, end_s=end, text=text))
    return Transcript(language=language, words=words, spans=spans, duration_s=duration,
                      version="srt-phrase-timing-1")


def speech_model_digest(settings: Settings) -> str:
    model_dir = settings.resolve(settings.speech.model)
    if not all((model_dir / name).is_file() for name in ("model.bin", "tokenizer.json", "config.json")):
        raise ReviewRequired("complete local Whisper model required, including tokenizer.json")
    return stable_id({p.name: sha256_file(p) for p in sorted(model_dir.iterdir())
                      if p.is_file() and p.suffix in (".bin", ".json", ".txt")})


def extract_audio(settings: Settings, media_path: Path, destination: Path):
    # WAV has no start-time metadata. Fill any initial audio offset with silence
    # so audio-only extraction still uses the video's zero-based presentation time.
    run_command([settings.render.ffmpeg, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(media_path),
                 "-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "16000",
                 "-c:a", "pcm_s16le", str(destination)])


def transcribe(settings: Settings, media_path: Path, source_sha: str,
               duration: float, supplied: Path | None = None,
               model_digest: str | None = None) -> Transcript:
    speech = settings.speech
    if not supplied and model_digest is None:
        model_digest = speech_model_digest(settings)
    key = stable_id({"source": source_sha, "speech": speech.model_dump(), "version": "speech-1",
                     "model_digest": model_digest,
                     "provided": sha256_file(supplied) if supplied else None})
    cache = settings.resolve(settings.paths.cache) / "transcripts" / f"{key}.json"
    if cache.exists():
        return Transcript.model_validate_json(cache.read_text(encoding="utf-8"))
    if supplied:
        if supplied.suffix.lower() == ".srt":
            result = load_subtitles(supplied, duration, speech.language or "und")
        else:
            result = Transcript.model_validate_json(supplied.read_text(encoding="utf-8"))
        if abs(result.duration_s - duration) > 0.25:
            raise ReviewRequired("supplied transcript duration differs from source")
    else:
        # A path is mandatory; no implicit remote model download during processing.
        model_dir = settings.resolve(speech.model)
        if not model_dir.is_dir():
            raise ReviewRequired("Whisper model missing; download locally during setup")
        check_speech_runtime()
        from faster_whisper import WhisperModel
        audio = cache.with_suffix(".wav")
        audio.parent.mkdir(parents=True, exist_ok=True)
        extract_audio(settings, media_path, audio)
        model = None
        try:
            model = WhisperModel(str(model_dir), device=speech.device, compute_type=speech.compute_type,
                                 cpu_threads=speech.cpu_threads, local_files_only=True)
            segments, info = model.transcribe(str(audio), language=speech.language,
                                              beam_size=speech.beam_size, vad_filter=True,
                                              word_timestamps=True, condition_on_previous_text=False)
            # Consume the generator before unloading. faster-whisper maps VAD times back
            # to the original audio; the extracted WAV was never silence-trimmed.
            words = [Word(start_s=float(w.start), end_s=float(w.end), text=w.word)
                     for segment in segments for w in (segment.words or [])
                     if w.end > w.start and w.word.strip()]
            result = Transcript(language=info.language, words=words, spans=spans_from_words(words),
                                duration_s=duration, version="whisper-1:" + model_digest)
        finally:
            del model
            gc.collect()
            audio.unlink(missing_ok=True)
    atomic_json(cache, result.model_dump())
    return result


def validate_candidates(data: CandidateList, transcript: Transcript, settings: Settings,
                        allowed_ids: set[str] | None = None) -> list[Candidate]:
    spans = {s.id: s for s in transcript.spans}
    indexes = {s.id: i for i, s in enumerate(transcript.spans)}
    result = []
    for item in data.clips:
        ids = (item.start_id, item.end_id, item.hook_id, item.payoff_id)
        if any(x not in spans or (allowed_ids is not None and x not in allowed_ids) for x in ids):
            raise ReviewRequired("unknown or out-of-window transcript ID")
        first, last = spans[item.start_id], spans[item.end_id]
        a, b, h, p = (indexes[x] for x in ids)
        if not (a <= h <= p <= b):
            raise ReviewRequired("reversed interval or out-of-range hook/payoff")
        if item.start_s != first.start_s or item.end_s != last.end_s:
            raise ReviewRequired("timestamp invented or changed; exact canonical values required")
        duration = last.end_s - first.start_s
        if not settings.brain.min_clip_s - 1e-8 <= duration <= settings.brain.max_clip_s + 1e-8:
            raise ReviewRequired("clip duration outside editing bounds")
        if first.start_s >= last.end_s or last.end_s > transcript.duration_s:
            raise ReviewRequired("invalid source interval")
        result.append(item.model_copy(update={"start_s": first.start_s, "end_s": last.end_s}))
    return result


def transcript_windows(transcript: Transcript, settings: Settings) -> list[list[Span]]:
    # UTF-8 byte upper bound is conservative for tokenization. Include serialized schema
    # and prompt in the budget, leaving 2k tokens for output. No truncation of a span.
    overhead = len((prompt("selection") + json.dumps(CandidateList.model_json_schema())).encode())
    budget = min(settings.brain.window_chars, settings.brain.context_tokens - overhead - 2200)
    if budget < 500:
        raise ReviewRequired("context too small for schema, prompt and output reserve")
    spans, windows, start = transcript.spans, [], 0
    while start < len(spans):
        end, size = start, 2
        while end < len(spans):
            piece = len(json.dumps(spans[end].model_dump(), ensure_ascii=False).encode()) + 2
            if size + piece > budget:
                break
            size += piece
            end += 1
        if end == start:
            raise ReviewRequired("single transcript span exceeds context budget")
        windows.append(spans[start:end])
        if end == len(spans):
            break
        # Overlap at least maximum editing duration when space permits. Always progress.
        overlap = end - 1
        while overlap > start + 1 and (spans[end - 1].end_s - spans[overlap].start_s < settings.brain.max_clip_s
                                       or end - overlap < settings.brain.overlap_spans):
            overlap -= 1
        start = max(start + 1, overlap)
    return windows


class LocalLLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = requests.Session()
        self.http.trust_env = False  # No environment proxy can receive transcripts.
        self.base = settings.brain.ollama_url.rstrip("/")
        response = self.http.post(self.base + "/api/show", json={"model": settings.brain.model},
                                  timeout=15, allow_redirects=False)
        if response.status_code != 200:
            raise ReviewRequired("local Ollama model unavailable")
        show = response.json()
        if (show.get("remote_host") or show.get("remote_model") or
                show.get("details", {}).get("format") != "gguf" or not show.get("model_info")):
            raise ReviewRequired("refusing model without local GGUF weights")
        tags = self.http.get(self.base + "/api/tags", timeout=15, allow_redirects=False)
        tags.raise_for_status()
        self.digest = next((m["digest"] for m in tags.json().get("models", [])
                            if m.get("name") == settings.brain.model), None)
        if not self.digest:
            raise ReviewRequired("could not identify installed model digest")

    def structured(self, system: str, data: dict, contract: type[T], validator=None) -> T:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(data, ensure_ascii=False)}]
        for attempt in range(self.settings.brain.correction_retries + 1):
            response = self.http.post(self.base + "/api/chat", json={
                "model": self.settings.brain.model, "messages": messages, "stream": False,
                "format": contract.model_json_schema(), "think": False, "keep_alive": 0,
                "options": {"temperature": 0, "num_ctx": self.settings.brain.context_tokens,
                            "num_predict": 1800}}, timeout=self.settings.brain.timeout_s,
                allow_redirects=False)
            if response.status_code != 200:
                raise ReviewRequired("local Ollama request failed")
            try:
                raw = response.json()["message"]["content"]
                parsed = contract.model_validate_json(raw)
                if validator:
                    validator(parsed)
                return parsed
            except (ValidationError, ValueError, KeyError, TypeError) as error:
                if attempt == self.settings.brain.correction_retries:
                    raise ReviewRequired("local model repeatedly failed output validation") from error
                # Do not append the invalid completion: it can exceed the reserved context.
                messages = messages[:2]
                messages.append({"role": "user", "content": "Correct the JSON against the original data and schema. "
                                 "Copy exact boundary IDs and times. Return empty clips if needed. Error category: "
                                 + type(error).__name__})
        raise AssertionError("unreachable")


def select_clips(settings: Settings, transcript: Transcript, llm: LocalLLM | None,
                 supplied: Path | None = None) -> list[Candidate]:
    if supplied:
        candidates = validate_candidates(CandidateList.model_validate_json(supplied.read_text(encoding="utf-8")),
                                         transcript, settings)
    else:
        if llm is None:
            raise ReviewRequired("local LLM required for automatic selection")
        candidates = []
        for window in transcript_windows(transcript, settings):
            allowed = {s.id for s in window}
            result = llm.structured(prompt("selection"), {
                "editing_min_seconds": settings.brain.min_clip_s,
                "editing_max_seconds": settings.brain.max_clip_s,
                "spans": [s.model_dump() for s in window]}, CandidateList,
                lambda x: validate_candidates(x, transcript, settings, allowed))
            candidates.extend(validate_candidates(result, transcript, settings, allowed))
    # Global editorial estimate ordering, overlap suppression across windows.
    chosen = []
    for item in sorted(candidates, key=lambda c: (-c.score, c.start_s)):
        if any(max(0, min(c.end_s, item.end_s) - max(c.start_s, item.start_s)) /
               min(c.end_s - c.start_s, item.end_s - item.start_s) > 0.6 for c in chosen):
            continue
        chosen.append(item)
        if len(chosen) >= settings.brain.max_clips:
            break
    return chosen


def clip_text(transcript: Transcript, candidate: Candidate) -> str:
    return join_words([w for w in transcript.words if w.end_s > candidate.start_s and w.start_s < candidate.end_s])


def generate_metadata(settings: Settings, transcript: Transcript, candidate: Candidate,
                      llm: LocalLLM | None, supplied: Path | None = None) -> Metadata:
    if supplied:
        return Metadata.model_validate_json(supplied.read_text(encoding="utf-8"))
    if llm is None:
        raise ReviewRequired("local LLM required for metadata")
    return llm.structured(prompt("metadata"), {"clip_transcript": clip_text(transcript, candidate)}, Metadata)
