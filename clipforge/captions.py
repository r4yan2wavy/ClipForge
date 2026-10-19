"""Clip-relative captions from canonical, absolute word times.

Only the renderer's own ``\\N`` line breaks enter ASS markup. Literal braces
and backslashes in untrusted transcript text use visible full-width equivalents.
The spoken words and punctuation remain readable, without executable ASS tags.
"""
from __future__ import annotations

import html
import math
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .common import Transcript

CAPTION_VERSION = "1"


@dataclass(frozen=True)
class Caption:
    start_s: float
    end_s: float
    text: str


def _plain(text: str) -> str:
    # Embedded newlines, C0 controls, or a cue separator cannot create records.
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())


def _join(words: list[str]) -> str:
    text = " ".join(words)
    # Whisper words often include punctuation; creator transcripts may separate it.
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def group_captions(transcript: Transcript, start_s: float, end_s: float,
                   *, max_chars: int = 62, max_words: int = 10,
                   max_duration_s: float = 3.5, pause_s: float = 0.6,
                   line_width: int = 32) -> list[Caption]:
    """Intersect words, clamp at the cut, then rebase onto the rendered timeline."""
    if not all(math.isfinite(v) for v in (start_s, end_s)) or not 0 <= start_s < end_s:
        raise ValueError("invalid clip interval")
    if max_chars < 1 or max_words < 1 or line_width < 1 or max_duration_s <= 0:
        raise ValueError("invalid caption grouping settings")
    result: list[Caption] = []
    current: list[str] = []
    cue_start = cue_end = 0.0

    def flush() -> None:
        nonlocal current
        if current and cue_end > cue_start:
            # No word is truncated. An unusually long word may exceed line_width.
            wrapped = textwrap.fill(_join(current), width=line_width,
                                    break_long_words=False, break_on_hyphens=False)
            result.append(Caption(cue_start - start_s, cue_end - start_s, wrapped))
        current = []

    for word in transcript.words:
        left, right = max(start_s, word.start_s), min(end_s, word.end_s)
        text = _plain(word.text)
        if right <= left or not text:
            continue
        if current and (left - cue_end > pause_s or right - cue_start > max_duration_s
                        or len(current) >= max_words
                        or len(_join(current + [text])) > max_chars):
            flush()
        if not current:
            cue_start = left
        current.append(text)
        cue_end = max(cue_end if len(current) > 1 else left, right)
        if re.search(r"[.!?][\"'\u2019\u201d]*$", text):
            flush()
    flush()
    return result


def _time(value: float, *, ass: bool = False) -> str:
    scale = 100 if ass else 1000
    ticks = max(0, round(value * scale))
    seconds, fraction = divmod(ticks, scale)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}" if ass
            else f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}")


def srt_text(captions: list[Caption]) -> str:
    # HTML escaping preserves literal <b> etc. for players that interpret SRT HTML.
    return "\n\n".join(
        f"{i}\n{_time(c.start_s)} --> {_time(c.end_s)}\n{html.escape(c.text, quote=False)}"
        for i, c in enumerate(captions, 1)
        if round(c.end_s * 1000) > round(c.start_s * 1000)
    ) + "\n"


def escape_ass(text: str) -> str:
    text = text.replace("\\", "\uff3c").replace("{", "\uff5b").replace("}", "\uff5d")
    return text.replace("\r", "").replace("\n", r"\N")


def ass_text(captions: list[Caption], *, width: int = 1080, height: int = 1920,
             font: str = "Arial", font_size: int = 64, margin_v: int = 230) -> str:
    if not font or any(c in font for c in ",\r\n\\{}"):
        raise ValueError("font must be a plain ASS font-family name")
    if width <= 0 or height <= 0 or font_size <= 0 or margin_v < 0:
        raise ValueError("invalid ASS styling")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "".join(
        f"Dialogue: 0,{_time(c.start_s, ass=True)},{_time(c.end_s, ass=True)},Default,,0,0,0,,{escape_ass(c.text)}\n"
        for c in captions if round(c.end_s * 100) > round(c.start_s * 100)
    )


def write_captions(transcript: Transcript, start_s: float, end_s: float, out_dir: Path,
                   render) -> dict[str, Path]:
    captions = group_captions(transcript, start_s, end_s)
    if not captions:
        raise ValueError("selected interval has no captioned speech; review transcript")
    out_dir.mkdir(parents=True, exist_ok=True)
    srt, ass = out_dir / "captions.srt", out_dir / "captions.ass"
    srt.write_text(srt_text(captions), encoding="utf-8")
    ass.write_text(ass_text(captions, width=render.width, height=render.height,
                            font=render.font, font_size=render.font_size,
                            margin_v=render.margin_v), encoding="utf-8")
    return {"srt": srt, "ass": ass}
