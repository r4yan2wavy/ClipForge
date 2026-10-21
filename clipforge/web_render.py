"""Operator-selected cuts for the local studio, without fabricated AI transcripts."""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import textwrap
from pathlib import Path
from typing import Callable

from .captions import Caption, _plain, ass_text, srt_text
from .common import Settings, atomic_json, run_command, sha256_file
from .factory import _video_graph, probe_media, verify_output
from .job_control import checkpoint, controlled, ffmpeg_progress, report

MANUAL_RENDER_VERSION = "2"


def render_manual(settings: Settings, media: Path, out_dir: Path, *, start_s: float,
                  end_s: float, framing: str, caption_text: str = "",
                  on_stage: Callable[[str], None] | None = None) -> dict:
    """Render an explicit continuous cut; optional text is a static manual overlay.

    This path never creates a speech transcript or attributes timings to speech.
    A silent audio stream is added to video-only recordings for MP4 compatibility.
    """
    def stage(name: str):
        report(name)
        if on_stage:
            on_stage(name)

    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in (start_s, end_s))
            or not 0 <= start_s < end_s or not 1 <= end_s - start_s <= 180):
        raise ValueError("Choose a clip between 1 and 180 seconds within the video.")
    if framing not in {"center_crop", "fit", "blur"}:
        raise ValueError("Choose one of the three framing options.")
    if not isinstance(caption_text, str) or len(caption_text) > 500:
        raise ValueError("Keep the manual text overlay within 500 characters.")
    media, out_dir = media.resolve(), out_dir.resolve()
    if media == out_dir / "clip.mp4":
        raise ValueError("Choose a separate output folder.")
    stage("preparing")
    info = probe_media(settings, media)
    if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
        raise ValueError("The selected file has no video stream.")
    if end_s > float(info["format"]["duration"]) + 0.000001:
        raise ValueError("The clip ends after the source video.")
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    duration = end_s - start_s
    text = _plain(caption_text)
    captions = ([Caption(0, duration, textwrap.fill(text, width=32,
                 break_long_words=False, break_on_hyphens=False))] if text else [])
    spec = {"manual_renderer_version": MANUAL_RENDER_VERSION,
            "source_sha256": sha256_file(media), "start_s": start_s, "end_s": end_s,
            "framing": framing, "caption_text": text, "caption_kind": "manual_overlay",
            "render": settings.render.model_dump(), "source_has_audio": has_audio,
            "encoder_version": run_command([settings.render.ffmpeg, "-version"]).stdout.splitlines()[0]}
    out_dir.mkdir(parents=True, exist_ok=True)
    output, cache = out_dir / "clip.mp4", out_dir / "render_cache.json"
    paths = {"srt": out_dir / "captions.srt", "ass": out_dir / "captions.ass"}

    def result(digest: str) -> dict:
        return {"video_path": str(output), "render_sha256": digest,
                "caption_path": str(paths[settings.render.caption_format]) if text else None,
                "caption_srt_path": str(paths["srt"]) if text else None,
                "render_spec": spec, "width": settings.render.width,
                "height": settings.render.height, "duration_s": duration}

    if cache.exists() and output.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if (saved.get("spec") == spec and saved.get("sha256") == sha256_file(output)
                    and saved.get("caption_sha256") == {
                        k: sha256_file(p) for k, p in paths.items() if text and p.is_file()}
                    and (not text or all(p.is_file() for p in paths.values()))):
                stage("ready")
                return result(saved["sha256"])
        except (OSError, ValueError):
            pass
    with tempfile.TemporaryDirectory(prefix=".manual-", dir=out_dir) as temporary:
        temporary = Path(temporary)
        (temporary / "captions.ass").write_text(ass_text(captions,
            width=settings.render.width, height=settings.render.height,
            font=settings.render.font, font_size=settings.render.font_size,
            margin_v=settings.render.margin_v), encoding="utf-8")
        (temporary / "captions.srt").write_text(srt_text(captions), encoding="utf-8")
        args = [settings.render.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", f"{start_s:.9f}", "-t", f"{duration:.9f}", "-protocol_whitelist", "file,pipe", "-i", str(media)]
        if not has_audio:
            args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        video_graph = _video_graph(framing, settings.render.width, settings.render.height,
                                   settings.render.fps)
        if not text:
            video_graph = video_graph.replace(",ass=filename=captions.ass", "")
        # Preserve the source level with a peak limiter. Loudness normalization
        # on fully silent tracks can emit nonfinite samples in some FFmpeg builds.
        # Fill both initial gaps and short audio tails to retain the video timeline.
        audio_input = "[0:a:0]" if has_audio else "[1:a:0]"
        audio_graph = (audio_input + f"aresample=async=1:first_pts=0,atrim=duration={duration:.9f},"
            "asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad=whole_dur={duration:.9f},atrim=duration={duration:.9f},"
            "alimiter=limit=0.95:level=false:latency=true[a]")
        args += ["-filter_complex", video_graph + ";" + audio_graph,
                 "-map", "[v]", "-map", "[a]", "-t", f"{duration:.9f}",
                 "-c:v", "libx264", "-preset", settings.render.preset,
                 "-crf", str(settings.render.crf), "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-b:a", "128k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                 "-map_metadata", "-1", "clip.mp4"]
        stage("rendering")
        report("rendering", 0.0, duration)
        if controlled():
            args[1:1] = ["-progress", "pipe:1", "-nostats"]
            run_command(args, cwd=temporary, on_stdout=ffmpeg_progress(duration))
        else:
            run_command(args, cwd=temporary)
        report("rendering", duration, duration)
        stage("verifying")
        verify_output(settings, temporary / "clip.mp4", duration)
        digest = sha256_file(temporary / "clip.mp4")
        checkpoint()
        os.replace(temporary / "clip.mp4", output)
        for key, path in paths.items():
            if text:
                shutil.copyfile(temporary / f"captions.{key}", path)
            else:
                path.unlink(missing_ok=True)
        atomic_json(cache, {"spec": spec, "sha256": digest,
                            "caption_sha256": {k: sha256_file(p) for k, p in paths.items() if text}})
    stage("ready")
    return result(digest)
