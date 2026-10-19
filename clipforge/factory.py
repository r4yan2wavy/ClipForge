"""FFmpeg assembly: a continuous source cut, vertical frame, captions and audio."""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from .captions import CAPTION_VERSION, write_captions
from .common import (Candidate, Settings, SourceManifest, Transcript, atomic_json,
                     run_command, sha256_file, stable_id)

RENDER_VERSION = "2"


def probe_media(settings: Settings, path: Path) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"missing local media: {path.name}")
    result = run_command([settings.render.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams",
                          "-show_format", "-of", "json", str(path)])
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("media has no valid finite duration")
    return data


def verify_output(settings: Settings, path: Path, expected_duration: float) -> dict:
    info = probe_media(settings, path)
    videos = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if len(videos) != 1 or len(audio) != 1:
        raise ValueError("render must contain exactly one video and one audio stream")
    video = videos[0]
    if (video.get("width"), video.get("height")) != (settings.render.width, settings.render.height):
        raise ValueError("incorrect rendered dimensions")
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise ValueError("incompatible rendered video codec or pixel format")
    if audio[0].get("codec_name") != "aac":
        raise ValueError("incompatible rendered audio codec")
    if abs(float(info["format"]["duration"]) - expected_duration) > 1 / settings.render.fps + 0.15:
        raise ValueError("rendered duration differs from the canonical cut")
    # Decode the entire output; container metadata alone cannot establish validity.
    run_command([settings.render.ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror",
                 "-i", str(path.resolve()), "-map", "0:v:0", "-map", "0:a:0",
                 "-f", "null", "-"])
    return info


def _canonical_interval(transcript: Transcript, candidate: Candidate) -> tuple[float, float]:
    spans = {s.id: s for s in transcript.spans}
    indices = {s.id: i for i, s in enumerate(transcript.spans)}
    if any(x not in spans for x in (candidate.start_id, candidate.end_id,
                                    candidate.hook_id, candidate.payoff_id)):
        raise ValueError("unknown transcript boundary ID")
    start, end = spans[candidate.start_id].start_s, spans[candidate.end_id].end_s
    if candidate.start_s != start or candidate.end_s != end:
        raise ValueError("candidate timestamps must exactly match canonical boundaries")
    first, last = indices[candidate.start_id], indices[candidate.end_id]
    if first > last or end <= start:
        raise ValueError("reversed candidate interval")
    if not all(first <= indices[x] <= last for x in (candidate.hook_id, candidate.payoff_id)):
        raise ValueError("candidate anchors outside selected interval")
    return start, end


def _video_graph(framing: str, width: int, height: int, fps: int) -> str:
    # These values are application enums/numbers; no AI/transcript content is code.
    base = "[0:v:0]setpts=PTS-STARTPTS"
    fill = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if framing == "center_crop":
        graph = f"{base},{fill}[framed]"
    elif framing == "fit":
        graph = f"{base},{fit},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black[framed]"
    elif framing == "blur":
        graph = (f"{base},split=2[bg0][fg0];[bg0]{fill},boxblur=20:2[bg];"
                 f"[fg0]{fit}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[framed]")
    else:
        raise ValueError("unknown framing mode")
    return graph + f";[framed]setsar=1,fps={fps},ass=filename=captions.ass,format=yuv420p[v]"


def _audio_graph(settings: Settings, duration: float, music: bool) -> str:
    render = settings.render
    # Input seeking keeps the audio's offset relative to the selected video cut.
    # Fill that initial gap before resetting PTS, preserving caption alignment.
    speech = (f"[0:a:0]aresample=async=1:first_pts=0,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,"
              "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo")
    loudness = f"loudnorm=I={render.loudness_lufs}:TP=-1.5:LRA=11,aresample=48000[a]"
    if not music:
        return speech + "[voice];[voice]" + loudness
    fade = min(1.0, duration / 4)
    music_graph = (f"[1:a:0]atrim=duration={duration:.9f},asetpts=PTS-STARTPTS,"
                   "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                   f"volume={render.music_gain_db}dB,afade=t=in:st=0:d={fade:.9f},"
                   f"afade=t=out:st={duration-fade:.9f}:d={fade:.9f}[music]")
    if render.ducking:
        return (speech + ",asplit=2[voice][key];" + music_graph + ";"
                "[music][key]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=250[duck];"
                "[voice][duck]amix=inputs=2:duration=first:normalize=0[mix];[mix]" + loudness)
    return (speech + "[voice];" + music_graph + ";"
            "[voice][music]amix=inputs=2:duration=first:normalize=0[mix];[mix]" + loudness)


def render_clip(settings: Settings, media_path: Path, transcript: Transcript,
                candidate: Candidate, manifest: SourceManifest, out_dir: Path) -> dict:
    """Render only canonical cuts; a completed identical render is reused by hash."""
    media_path, out_dir = media_path.resolve(), out_dir.resolve()
    start, end = _canonical_interval(transcript, candidate)
    # Only arithmetic on already exact canonical timestamps gets an ULP-scale
    # tolerance; a supplied invented timestamp still fails equality above.
    if not settings.brain.min_clip_s - 1e-8 <= end - start <= settings.brain.max_clip_s + 1e-8:
        raise ValueError("canonical cut violates configured editing duration")
    source_info = probe_media(settings, media_path)
    streams = source_info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise ValueError("source has no video stream")
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError("source has no audio; route to editorial review")
    if end > float(source_info["format"]["duration"]) + 0.02:
        raise ValueError("canonical cut extends past source media")
    music_path = settings.resolve(manifest.music_path) if manifest.music_path else None
    if media_path == out_dir / "clip.mp4" or music_path == out_dir / "clip.mp4":
        raise ValueError("render output must be separate from source media and music")
    if music_path:
        if not manifest.music_license_reference or not manifest.music_license_reference.strip():
            raise ValueError("music requires a verified license reference")
        if not any(s.get("codec_type") == "audio" for s in probe_media(settings, music_path)["streams"]):
            raise ValueError("music has no audio stream")
    duration = end - start
    spec = {
        "renderer_version": RENDER_VERSION, "caption_version": CAPTION_VERSION,
        "encoder_version": run_command([settings.render.ffmpeg, "-version"]).stdout.splitlines()[0],
        "source_sha256": sha256_file(media_path), "start_id": candidate.start_id,
        "end_id": candidate.end_id, "start_s": start, "end_s": end,
        "transcript_sha256": stable_id(transcript.model_dump(mode="json")),
        "render": settings.render.model_dump(mode="json"), "framing": manifest.framing,
        "music_sha256": sha256_file(music_path) if music_path else None,
        "music_license_reference": manifest.music_license_reference,
        "music_attribution": manifest.music_attribution,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    output, cache = out_dir / "clip.mp4", out_dir / "render_cache.json"
    paths = {"srt": out_dir / "captions.srt", "ass": out_dir / "captions.ass"}

    def result(digest: str) -> dict:
        return {"video_path": str(output), "caption_path": str(paths[settings.render.caption_format]),
                "caption_srt_path": str(paths["srt"]), "caption_ass_path": str(paths["ass"]),
                "render_sha256": digest, "render_spec": spec}

    if cache.is_file() and output.is_file() and all(p.is_file() for p in paths.values()):
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            if (old.get("spec") == spec and old.get("sha256") == sha256_file(output)
                    and old.get("caption_sha256") == {k: sha256_file(p) for k, p in paths.items()}):
                return result(old["sha256"])
        except (ValueError, OSError):
            pass
    # A private cwd and fixed caption filename avoid FFmpeg filter-path escaping,
    # including Windows drives, apostrophes and arbitrary source/inbox names.
    with tempfile.TemporaryDirectory(prefix=".render-", dir=out_dir) as temporary:
        stage = Path(temporary)
        written = write_captions(transcript, start, end, stage, settings.render)
        args = [settings.render.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-ss", f"{start:.9f}", "-t", f"{duration:.9f}", "-protocol_whitelist", "file,pipe", "-i", str(media_path)]
        if music_path:
            args += ["-stream_loop", "-1", "-protocol_whitelist", "file,pipe", "-i", str(music_path)]
        graph = (_video_graph(manifest.framing, settings.render.width, settings.render.height,
                              settings.render.fps) + ";" + _audio_graph(settings, duration, bool(music_path)))
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{duration:.9f}",
                 "-c:v", "libx264", "-preset", settings.render.preset, "-crf", str(settings.render.crf),
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                 "-ac", "2", "-movflags", "+faststart", "-map_metadata", "-1", "clip.mp4"]
        run_command(args, cwd=stage)
        rendered = stage / "clip.mp4"
        verify_output(settings, rendered, duration)
        digest = sha256_file(rendered)
        for key, path in written.items():
            shutil.copyfile(path, paths[key])
        os.replace(rendered, output)
        atomic_json(cache, {"spec": spec, "sha256": digest,
                            "caption_sha256": {k: sha256_file(p) for k, p in paths.items()}})
    return result(digest)
