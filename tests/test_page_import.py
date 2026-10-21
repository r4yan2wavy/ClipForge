"""Offline importer isolation, bounds, cleanup and worker regressions."""
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipforge import page_import as page


YOUTUBE = "https://www.youtube.com/watch?v=abcdefghijk"


def test_display_progress_uses_current_stream_bytes_not_wire_overhead(tmp_path, monkeypatch):
    monkeypatch.setattr(page, "_runtime", lambda: ("node", "node"))
    transfer = page._Transfer(tmp_path)
    transfer.received = 80
    hook = page._options(tmp_path, "ffmpeg", transfer)["progress_hooks"][0]
    hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})
    saved = page._read_json(tmp_path / "progress.json")
    assert saved["stage"] == "downloading"
    assert saved["received"] == 10 and saved["total"] == 100
    assert transfer.received == 80  # The separate network safety cap stays intact.
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})
    assert page._read_json(tmp_path / "progress.json")["stage"] == "merging"


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Importer tests must not contact real video services")
    monkeypatch.setattr(page.socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(page.socket.socket, "connect", forbidden)


@pytest.mark.parametrize("source, canonical", [
    ("https://youtu.be/abcdefghijk?tracking=PRIVATE", YOUTUBE),
    ("https://www.youtube.com/watch?v=abcdefghijk&list=PRIVATE", YOUTUBE),
    ("https://www.twitch.tv/videos/123?tracking=PRIVATE", "https://www.twitch.tv/videos/123"),
    ("https://www.twitch.tv/owner/clip/ClipSlug", "https://clips.twitch.tv/ClipSlug"),
    ("https://clips.twitch.tv/ClipSlug?tracking=PRIVATE", "https://clips.twitch.tv/ClipSlug"),
    ("https://kick.com/owner/videos/5c697a87-afce-4256-b01f-3c8fe71ef5cb", "https://kick.com/owner/videos/5c697a87-afce-4256-b01f-3c8fe71ef5cb"),
    ("https://kick.com/owner?clip=clip_01GYXV&tracking=PRIVATE", "https://kick.com/owner/clips/clip_01GYXV"),
])
def test_only_specific_provider_videos_are_canonicalized(source, canonical):
    assert page.supported_page(source) == canonical


@pytest.mark.parametrize("source", [
    "http://www.youtube.com/watch?v=abcdefghijk", "https://www.youtube.com/playlist?list=123",
    "https://www.youtube.com/@owner", "https://www.twitch.tv/owner", "https://kick.com/owner",
    "https://youtube.com.attacker.example/watch?v=abcdefghijk", "https://evil.youtube.com/watch?v=abcdefghijk",
    "https://127.0.0.1/video.mp4", "https://user:secret@www.youtube.com/watch?v=abcdefghijk",
    "https://www.youtube.com:8443/watch?v=abcdefghijk", "https://files.example.com/video.mp4",
])
def test_channels_playlists_and_unsafe_urls_are_rejected(source):
    with pytest.raises(page.LinkError):
        page.supported_page(source)


def test_worker_blocks_private_dns_and_non_https_ports(monkeypatch, tmp_path):
    monkeypatch.setattr(page.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
    ])
    original = page.socket.getaddrinfo
    transfer = page._Transfer(tmp_path)
    with page._network_guard(transfer):
        with pytest.raises(page.LinkError, match="public internet"):
            socket.getaddrinfo("media.example.com", 443)
        with pytest.raises(page.LinkError, match="port"):
            socket.getaddrinfo("media.example.com", 80)
        with pytest.raises(page.LinkError, match="public internet"):
            socket.socket.connect(SimpleNamespace(family=socket.AF_INET), ("127.0.0.1", 443))
    assert page.socket.getaddrinfo is original


def test_worker_pins_public_dns_to_numeric_socket_destination(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(page.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    monkeypatch.setattr(page.socket.socket, "connect", lambda sock, address: calls.append(address))
    with page._network_guard(page._Transfer(tmp_path)):
        page.socket.socket.connect(SimpleNamespace(family=socket.AF_INET), ("media.example.com", 443))
    assert calls == [("8.8.8.8", 443)]


def test_http_reads_share_total_byte_limit_and_restore_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(page, "MAX_BYTES", 5)
    monkeypatch.setattr(page.http.client.HTTPResponse, "read", lambda response, amount=None: b"abc")
    original = page.http.client.HTTPResponse.read
    transfer = page._Transfer(tmp_path)
    with page._network_guard(transfer):
        assert page.http.client.HTTPResponse.read(None, 3) == b"abc"
        with pytest.raises(page.LinkError, match="10 GiB"):
            page.http.client.HTTPResponse.read(None, 3)
    assert transfer.received == 6 and page.http.client.HTTPResponse.read is original


def test_nested_readinto_counts_bytes_once(monkeypatch, tmp_path):
    monkeypatch.setattr(page.http.client.HTTPResponse, "read", lambda response, amount=None: b"abc")
    def readinto(response, buffer):
        data = page.http.client.HTTPResponse.read(response, len(buffer))
        buffer[:len(data)] = data
        return len(data)
    monkeypatch.setattr(page.http.client.HTTPResponse, "readinto", readinto)
    transfer = page._Transfer(tmp_path)
    with page._network_guard(transfer):
        buffer = bytearray(4)
        assert page.http.client.HTTPResponse.readinto(None, buffer) == 3
    assert transfer.received == 3 and buffer == b"abc\x00"


def _fake_worker(monkeypatch, tmp_path, *, error=None):
    seen = {}
    class Downloader:
        def __init__(self, options):
            self.options = options
            seen.update(options)
            self._request_director = SimpleNamespace(handlers={
                "Urllib": SimpleNamespace(close=lambda: None),
                "Requests": SimpleNamespace(close=lambda: seen.setdefault("requests_closed", True)),
                "CurlCFFI": SimpleNamespace(close=lambda: seen.setdefault("curl_closed", True)),
            })
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            assert url == YOUTUBE and download is True
            assert tuple(self._request_director.handlers) == ("Urllib",)
            (tmp_path / "source.mp4").write_bytes(b"synthetic downloaded media")
            # Real YoutubeDL raises after a successful download when max_downloads=1.
            if self.options.get("max_downloads") == 1:
                raise RuntimeError("MaxDownloadsReached")
            if error:
                raise RuntimeError(error)
            return {"title": "A sample title", "vcodec": "avc1.640028", "acodec": "mp4a.40.2"}
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=Downloader))
    monkeypatch.setitem(sys.modules, "yt_dlp.globals", SimpleNamespace(plugin_dirs=SimpleNamespace(value=["default"])))
    def normalize(source, destination, ffmpeg, transfer, **kwargs):
        seen["copy_streams"] = kwargs["copy_streams"]
        destination.write_bytes(source.read_bytes())
    monkeypatch.setattr(page, "_normalize", normalize)
    page._write_json(tmp_path / "spec.json", {"url": YOUTUBE, "ffmpeg": "ffmpeg"})
    return seen


def test_successful_single_download_is_not_treated_as_maxdownloads_failure(monkeypatch, tmp_path):
    options = _fake_worker(monkeypatch, tmp_path)
    assert page._worker(tmp_path / "spec.json") == 0
    assert page._read_json(tmp_path / "result.json") == {"ok": True, "title": "A sample title"}
    assert options["copy_streams"] is True
    assert options["requests_closed"] and options["curl_closed"]
    assert options["proxy"] == "" and options["usenetrc"] is False
    assert options["cookiefile"] is None and options["cookiesfrombrowser"] is None
    assert options["remote_components"] == [] and options["cachedir"] is False
    assert options["external_downloader"]["default"] == "native"


def test_provider_error_never_exposes_signed_url(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, error="https://secret.googlevideo.com/video?token=PRIVATE")
    assert page._worker(tmp_path / "spec.json") == 1
    result = page._read_json(tmp_path / "result.json")
    assert result["ok"] is False and "PRIVATE" not in result["error"] and "googlevideo" not in result["error"]


def test_live_and_protected_recordings_rejected_before_download(tmp_path):
    reject = page._options(tmp_path, "ffmpeg", page._Transfer(tmp_path))["match_filter"]
    assert reject({"is_live": True})
    assert reject({"live_status": "is_upcoming"})
    assert reject({"has_drm": True})
    assert reject({"_type": "playlist"})
    assert reject({"live_status": "was_live"}) is None


def _supervisor(monkeypatch, tmp_path, *, running=False, success=True):
    seen = {}
    monkeypatch.setattr(page, "importer_status", lambda ffmpeg: {"ok": True, "youtube_ready": True})
    monkeypatch.setattr(page.time, "sleep", lambda seconds: None)
    class Process:
        pid = 12345
        returncode = None if running else (0 if success else 1)
        def __init__(self, command, **kwargs):
            seen.update(command=command, options=kwargs, process=self)
            self.workspace = Path(command[-1]).parent
            if not running:
                (self.workspace / "result.mp4").write_bytes(b"finished MP4")
                page._write_json(self.workspace / "result.json", {"ok": success, "title": "Finished clip"})
        def poll(self):
            return self.returncode
    def stop(process):
        seen["stopped"] = True
        if process.returncode is None:
            process.returncode = 1
    monkeypatch.setattr(page.subprocess, "Popen", Process)
    monkeypatch.setattr(page, "_stop_tree", stop)
    return seen


def test_supervisor_publishes_only_requested_mp4_and_cleans_staging(monkeypatch, tmp_path):
    seen = _supervisor(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://PRIVATE")
    destination = tmp_path / "video.mp4"
    assert page.page_download(YOUTUBE, destination, ffmpeg="ffmpeg") == {"path": destination, "title": "Finished clip"}
    assert destination.read_bytes() == b"finished MP4"
    assert not list(tmp_path.glob(page.TEMP_PREFIX + "*"))
    assert seen["command"][1:4] == ["-m", "clipforge.page_import", "--worker"]
    assert "PRIVATE" not in str(seen["command"])
    assert "HTTPS_PROXY" not in seen["options"]["env"]


def test_cancellation_kills_worker_and_removes_staging(monkeypatch, tmp_path):
    seen = _supervisor(monkeypatch, tmp_path, running=True)
    destination = tmp_path / "video.mp4"
    with pytest.raises(page.LinkError, match="stopped"):
        page.page_download(YOUTUBE, destination, ffmpeg="ffmpeg", cancelled=lambda: True)
    assert seen["stopped"] and not destination.exists()
    assert not list(tmp_path.glob(page.TEMP_PREFIX + "*"))


def test_deadline_stops_worker_and_cleans_staging(monkeypatch, tmp_path):
    seen = _supervisor(monkeypatch, tmp_path, running=True)
    moments = iter([0, page.MAX_SECONDS + 1])
    monkeypatch.setattr(page.time, "monotonic", lambda: next(moments))
    with pytest.raises(page.LinkError, match="30 minutes"):
        page.page_download(YOUTUBE, tmp_path / "video.mp4", ffmpeg="ffmpeg")
    assert seen["stopped"] and not list(tmp_path.glob(page.TEMP_PREFIX + "*"))


def test_existing_source_is_never_overwritten(monkeypatch, tmp_path):
    destination = tmp_path / "video.mp4"
    destination.write_bytes(b"existing source")
    with pytest.raises(page.LinkError, match="new MP4"):
        page.page_download(YOUTUBE, destination, ffmpeg="ffmpeg")
    assert destination.read_bytes() == b"existing source"


def test_disk_shortage_stops_worker_without_retaining_partial_media(monkeypatch, tmp_path):
    seen = _supervisor(monkeypatch, tmp_path, running=True)
    monkeypatch.setattr(page.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(page.LinkError, match="disk space"):
        page.page_download(YOUTUBE, tmp_path / "video.mp4", ffmpeg="ffmpeg")
    assert seen["stopped"] and not list(tmp_path.glob(page.TEMP_PREFIX + "*"))


def test_normalization_remuxes_compatible_media_and_reencodes_only_on_failure(monkeypatch, tmp_path):
    commands = []
    source, destination = tmp_path / "source.mkv", tmp_path / "result.mp4"
    source.write_bytes(b"synthetic source")
    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.returncode = 1 if len(commands) == 1 else 0
            destination.write_bytes(b"synthetic MP4")
        def poll(self):
            return self.returncode
    monkeypatch.setattr(page.subprocess, "Popen", Process)
    page._normalize(source, destination, "ffmpeg", page._Transfer(tmp_path), copy_streams=True)
    assert len(commands) == 2 and ["-c", "copy"] == commands[0][commands[0].index("-c"):commands[0].index("-c") + 2]
    assert "libx264" in commands[1] and "aac" in commands[1]
    for command in commands:
        assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
        assert "hls" not in command[command.index("-format_whitelist") + 1]
        assert command[command.index("-i") + 1] == str(source)
