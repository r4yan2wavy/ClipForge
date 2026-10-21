"""Isolated, bounded yt-dlp imports with public-only pinned HTTPS transport."""
from __future__ import annotations

import contextlib
import http.client
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from .link_import import LinkError, MAX_BYTES, MAX_SECONDS, describe, parse_url

RESERVE_BYTES = 100 * 1024**2
TEMP_PREFIX = ".clipforge-link-import-"
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".ts"}
_ERROR = "This video could not be imported. It may be unavailable, private, restricted or temporarily unsupported. Update the link importer or attach an authorized local file."


def supported_page(value):
    """Canonicalize a supported recording/clip; reject channels and playlists."""
    parts, host = parse_url(value)
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
        return describe(value)["url"]
    path = parts.path.rstrip("/")
    if host in {"twitch.tv", "www.twitch.tv", "clips.twitch.tv"}:
        if host == "clips.twitch.tv" and re.fullmatch(r"/[A-Za-z0-9_-]{1,150}", path):
            return "https://clips.twitch.tv" + path
        clip = re.fullmatch(r"/[A-Za-z0-9_]+/clip/([A-Za-z0-9_-]{1,150})", path)
        if clip:
            return "https://clips.twitch.tv/" + clip[1]
        if re.fullmatch(r"/videos/\d{1,30}", path):
            return "https://www.twitch.tv" + path
    if host in {"kick.com", "www.kick.com"}:
        if re.fullmatch(r"/[A-Za-z0-9_-]+/videos/[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", path):
            return "https://kick.com" + path
        if re.fullmatch(r"/[A-Za-z0-9_-]+/clips/clip_[A-Za-z0-9_-]{1,150}", path):
            return "https://kick.com" + path
        clip = urllib.parse.parse_qs(parts.query).get("clip", [""])[0]
        if re.fullmatch(r"/[A-Za-z0-9_-]+", path) and re.fullmatch(r"clip_[A-Za-z0-9_-]{1,150}", clip):
            return f"https://kick.com{path}/clips/{clip}"
    raise LinkError("Paste a YouTube video, Twitch recording/clip, or Kick recording/clip link. Live channels and playlists are not supported.")


def _runtime():
    for name in ("deno", "node"):
        executable = shutil.which(name)
        if executable:
            return name, executable
    return None, None


def importer_status(ffmpeg):
    """Fast local dependency checks only; never contact a provider."""
    engine = importlib.util.find_spec("yt_dlp") is not None
    ejs = importlib.util.find_spec("yt_dlp_ejs") is not None
    runtime, runtime_path = _runtime()
    executable = shutil.which(str(ffmpeg)) or (str(ffmpeg) if Path(str(ffmpeg)).is_file() else None)
    try:
        version = importlib.metadata.version("yt-dlp") if engine else None
    except importlib.metadata.PackageNotFoundError:
        version = None
    ok = bool(engine and executable)
    message = ("Link importer is ready." if ok and runtime and ejs else
               "Link importer is installed. YouTube also needs the JavaScript helper and Node.js or Deno." if ok else
               "Install ClipForge's link import dependencies and FFmpeg. See the link import guide.")
    return {"ok": ok, "engine": "yt-dlp", "version": version,
            "youtube_ready": bool(ok and runtime and ejs), "runtime": runtime,
            "runtime_path": runtime_path, "message": message}


def _write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path):
    if not path.is_file() or path.stat().st_size > 8192:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class _Transfer:
    def __init__(self, workspace):
        self.workspace = workspace
        self.started = time.monotonic()
        self.received = 0
        self.last_progress = 0
        self.lock = threading.Lock()

    def add(self, count):
        with self.lock:
            self.received += count
            if self.received > MAX_BYTES:
                raise LinkError("This import exceeds the 10 GiB total download limit.")
        self.check()

    def check(self):
        if time.monotonic() - self.started >= MAX_SECONDS:
            raise LinkError("The link import exceeded 30 minutes. Try a shorter recording.")
        if shutil.disk_usage(self.workspace).free < RESERVE_BYTES:
            raise LinkError("There is not enough free disk space for this video.")

    def progress(self, stage, total=None, force=False, received=None):
        self.check()
        now = time.monotonic()
        if force or now - self.last_progress >= 0.4:
            self.last_progress = now
            _write_json(self.workspace / "progress.json", {
                # Wire bytes enforce the import cap, while yt-dlp's current
                # stream bytes measure display progress without page overhead.
                "stage": stage, "received": self.received if received is None else received,
                "total": total if isinstance(total, (int, float)) and 0 < total <= MAX_BYTES else None,
            })


@contextlib.contextmanager
def _network_guard(transfer):
    """Worker only: validate all DNS answers and pin each actual connection.

    Only urllib is enabled in yt-dlp, so its HTTP bodies pass through the byte
    counter. No browser credentials, proxies or C networking backends are used.
    """
    original_dns, original_connect = socket.getaddrinfo, socket.socket.connect
    original_read, original_readinto = http.client.HTTPResponse.read, http.client.HTTPResponse.readinto
    reading = threading.local()

    def checked_dns(host, port, *args, **kwargs):
        if str(port) != "443":
            raise LinkError("The video service attempted an unsupported network port.")
        addresses = original_dns(host, port, *args, **kwargs)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise LinkError("Video services must resolve only to public internet addresses.")
        return addresses

    def checked_connect(sock, address):
        if sock.family not in {socket.AF_INET, socket.AF_INET6} or len(address) < 2 or address[1] != 443:
            raise LinkError("The video service attempted an unsupported network destination.")
        try:
            numeric = ipaddress.ip_address(address[0])
        except ValueError:
            address = checked_dns(address[0], 443, sock.family, socket.SOCK_STREAM)[0][4]
        else:
            if not numeric.is_global:
                raise LinkError("Video services must connect only to public internet addresses.")
        transfer.check()
        return original_connect(sock, address)

    def counted_read(response, amount=None):
        outer = not getattr(reading, "active", False)
        reading.active = True
        try:
            whole = amount is None or amount < 0
            # Whole-response reads are metadata, never a multi-GiB allocation.
            data = original_read(response, 16 * 1024**2 + 1 if whole else amount)
            if whole and len(data) > 16 * 1024**2:
                raise LinkError("The video service returned an oversized metadata response.")
            if outer:
                transfer.add(len(data))
            return data
        finally:
            reading.active = not outer

    def counted_readinto(response, buffer):
        outer = not getattr(reading, "active", False)
        reading.active = True
        try:
            count = original_readinto(response, buffer)
            if outer:
                transfer.add(count or 0)
            return count
        finally:
            reading.active = not outer

    socket.getaddrinfo, socket.socket.connect = checked_dns, checked_connect
    http.client.HTTPResponse.read, http.client.HTTPResponse.readinto = counted_read, counted_readinto
    try:
        yield
    finally:
        socket.getaddrinfo, socket.socket.connect = original_dns, original_connect
        http.client.HTTPResponse.read, http.client.HTTPResponse.readinto = original_read, original_readinto


class _QuietLogger:
    def debug(self, message):
        pass

    info = warning = error = debug


def _options(workspace, ffmpeg, transfer):
    runtime, runtime_path = _runtime()

    def filter_recording(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            return "Only completed recordings can be imported."
        if info.get("has_drm"):
            return "Protected recordings cannot be imported."
        if info.get("_type") in {"playlist", "multi_video"}:
            return "Only a single video can be imported."
        return None

    def hook(value):
        amount = value.get("downloaded_bytes")
        measured = isinstance(amount, (int, float)) and not isinstance(amount, bool) and 0 <= amount <= MAX_BYTES
        transfer.progress("downloading" if value.get("status") != "finished" else "merging",
                          (value.get("total_bytes") or value.get("total_bytes_estimate")) if measured else None,
                          force=value.get("status") == "finished",
                          received=int(amount) if measured else None)

    return {
        "quiet": True, "no_warnings": True, "logger": _QuietLogger(),
        "noplaylist": True,
        "match_filter": filter_recording, "break_on_reject": True,
        "allowed_extractors": ["youtube", "twitch:vod", "twitch:clips", "kick:vod", "kick:clips"],
        "format": "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]",
        "merge_output_format": "mkv", "ffmpeg_location": str(ffmpeg),
        "outtmpl": str(workspace / "source.%(ext)s"),
        "paths": {"home": str(workspace), "temp": str(workspace)},
        "restrictfilenames": True, "windowsfilenames": True,
        "overwrites": False, "continuedl": False, "nopart": False,
        "cachedir": False, "cookiefile": None, "cookiesfrombrowser": None,
        "usenetrc": False, "proxy": "", "geo_verification_proxy": "",
        "socket_timeout": 15, "retries": 1, "fragment_retries": 1,
        "extractor_retries": 1, "file_access_retries": 0,
        "concurrent_fragment_downloads": 1, "max_filesize": MAX_BYTES,
        "allow_unplayable_formats": False, "hls_prefer_native": True,
        "external_downloader": {"default": "native", "m3u8": "native", "dash": "native"},
        "fixup": "never", "skip_unavailable_fragments": False,
        "js_runtimes": {runtime: {"path": runtime_path}} if runtime else {},
        "remote_components": [], "progress_hooks": [hook],
        "postprocessor_hooks": [lambda value: transfer.progress("merging")],
        "postprocessor_args": {"ffmpeg_i": ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,mpegts,aac"]},
    }


def _normalize(source, destination, ffmpeg, transfer, *, copy_streams=False):
    transfer.progress("verifying", force=True)
    for copy in ([True, False] if copy_streams else [False]):
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,mpegts,flv",
                   "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                   "-map_metadata", "-1", "-map_chapters", "-1"]
        command += (["-c", "copy"] if copy else
                    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"])
        command += ["-movflags", "+faststart", "-f", "mp4", str(destination)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while process.poll() is None:
                transfer.check()
                if destination.exists() and destination.stat().st_size > MAX_BYTES:
                    raise LinkError("The finished video exceeds the 10 GiB size limit.")
                time.sleep(0.2)
            transfer.check()
            if not process.returncode and destination.is_file() and 0 < destination.stat().st_size <= MAX_BYTES:
                return
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    raise LinkError("The downloaded audio and video could not be prepared for ClipForge.")


def _worker(spec_path):
    workspace = spec_path.parent
    try:
        spec = _read_json(spec_path)
        url = supported_page(spec["url"])
        transfer = _Transfer(workspace)
        transfer.progress("resolving", force=True)
        import yt_dlp
        from yt_dlp.globals import plugin_dirs
        plugin_dirs.value = []
        with _network_guard(transfer), yt_dlp.YoutubeDL(_options(workspace, spec["ffmpeg"], transfer)) as downloader:
            director = downloader._request_director
            if "Urllib" not in director.handlers:
                raise LinkError("The installed importer does not provide the required secure HTTP transport.")
            for name in tuple(director.handlers):
                if name != "Urllib":
                    director.handlers.pop(name).close()
            info = downloader.extract_info(url, download=True)
        if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"}:
            raise LinkError("Only a single completed recording can be imported.")
        candidates = [path for path in workspace.iterdir() if path.stem == "source" and path.suffix.lower() in _VIDEO_EXTENSIONS and path.is_file() and not path.is_symlink()]
        if len(candidates) != 1:
            raise LinkError("The video service did not provide one complete source video.")
        source = candidates[0]
        if source.stat().st_size > MAX_BYTES:
            raise LinkError("The downloaded video exceeds the 10 GiB size limit.")
        if shutil.disk_usage(workspace).free < source.stat().st_size + RESERVE_BYTES:
            raise LinkError("There is not enough free disk space to prepare this video.")
        video_codec = str(info.get("vcodec") or "").lower()
        audio_codec = str(info.get("acodec") or "").lower()
        compatible = video_codec.startswith(("avc1", "avc3", "h264")) and audio_codec.startswith(("mp4a", "aac", "none"))
        _normalize(source, workspace / "result.mp4", spec["ffmpeg"], transfer, copy_streams=compatible)
        title = " ".join(str(info.get("title") or "Imported video").split())[:200]
        _write_json(workspace / "result.json", {"ok": True, "title": title})
        return 0
    except LinkError as error:
        _write_json(workspace / "result.json", {"ok": False, "error": str(error)})
    except Exception:
        # Provider errors can contain signed URLs, tokens or local file paths.
        _write_json(workspace / "result.json", {"ok": False, "error": _ERROR})
    return 1


def _stop_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _workspace_size(workspace):
    total = 0
    for path in workspace.iterdir():
        if path.is_symlink() or path.is_dir():
            raise LinkError("The video importer created an unsupported staging entry.")
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            # The worker atomically replaces progress and media files.
            continue
    return total


def page_download(url, destination, *, ffmpeg, cancelled=lambda: False,
                  progress=lambda stage, received, total: None):
    """Import one link into a new MP4; stop and clean all staging on failure."""
    url = supported_page(url)
    destination = Path(destination)
    if destination.suffix.lower() != ".mp4" or destination.exists() or destination.is_symlink():
        raise LinkError("Choose a new MP4 destination for this video import.")
    health = importer_status(ffmpeg)
    if not health["ok"]:
        raise LinkError(health["message"])
    if "youtube.com" in url and not health["youtube_ready"]:
        raise LinkError("YouTube import needs the JavaScript helper and Node.js or Deno. Install the link import dependencies and see the link import guide.")
    started = time.monotonic()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX, dir=destination.parent) as temporary:
            workspace = Path(temporary)
            _write_json(workspace / "spec.json", {"url": url, "ffmpeg": str(ffmpeg)})
            environment = os.environ.copy()
            for key in tuple(environment):
                if key.lower().endswith("_proxy") or key.upper() in {"PYTHONSTARTUP", "YTDLP_PLUGIN_DIRS"}:
                    environment.pop(key, None)
            environment["PYTHONIOENCODING"] = "utf-8"
            arguments = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                         "stderr": subprocess.DEVNULL, "env": environment}
            if os.name == "nt":
                arguments["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                arguments["start_new_session"] = True
            process = subprocess.Popen([sys.executable, "-m", "clipforge.page_import", "--worker", str(workspace / "spec.json")], **arguments)
            last_progress = None
            try:
                while process.poll() is None:
                    if cancelled():
                        raise LinkError("The link import was stopped. You can retry it explicitly.")
                    if time.monotonic() - started >= MAX_SECONDS:
                        raise LinkError("The link import exceeded 30 minutes. Try a shorter recording.")
                    if shutil.disk_usage(workspace).free < RESERVE_BYTES:
                        raise LinkError("There is not enough free disk space for this video.")
                    if _workspace_size(workspace) > MAX_BYTES * 3 + 1024**2:
                        raise LinkError("The video import exceeded its staging size limit.")
                    state = _read_json(workspace / "progress.json")
                    if state and state != last_progress:
                        last_progress = state
                        stage = state.get("stage")
                        received, total = state.get("received"), state.get("total")
                        if isinstance(stage, str) and len(stage) <= 100 and isinstance(received, int) and 0 <= received <= MAX_BYTES:
                            progress(stage, received, total if isinstance(total, (int, float)) and 0 < total <= MAX_BYTES else None)
                    time.sleep(0.2)
            finally:
                _stop_tree(process)
            result = _read_json(workspace / "result.json")
            if process.returncode or result.get("ok") is not True:
                raise LinkError(result.get("error") if isinstance(result.get("error"), str) and len(result["error"]) <= 500 else _ERROR)
            video = workspace / "result.mp4"
            if video.is_symlink() or not video.is_file() or not 0 < video.stat().st_size <= MAX_BYTES:
                raise LinkError("The video importer did not finish a valid MP4 file.")
            if cancelled():
                raise LinkError("The link import was stopped. You can retry it explicitly.")
            # Windows rename refuses an existing target. On POSIX, link gives
            # the same no-overwrite guarantee without copying a multi-GiB file.
            if os.name == "nt":
                os.rename(video, destination)
            else:
                os.link(video, destination)
            return {"path": destination, "title": str(result.get("title") or "Imported video")[:200]}
    except LinkError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise LinkError("The video could not be imported. Check the connection, free disk space and link import setup, or attach a local file.") from None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker(Path(sys.argv[2])))
    raise SystemExit("This module is used by ClipForge's local link importer.")
