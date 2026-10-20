"""Bounded HTTPS file imports, link classification and official Twitch access.

The separate page_import module runs yt-dlp for supported public video pages.
DNS answers are checked and pinned to the TLS connection on every redirect.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

MAX_BYTES = 10 * 1024**3
MAX_SECONDS = 1800
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts"}
PLATFORM_CDNS = ("googlevideo.com", "ytimg.com", "twitchcdn.net", "jtvnw.net", "fbcdn.net", "cdninstagram.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokv.com", "byteoversea.com")


class LinkError(ValueError):
    pass


def _host_is(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def parse_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise LinkError("Enter a valid HTTPS video link without spaces or control characters.")
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username is not None or parts.password is not None or parts.port not in (None, 443):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if host.endswith(".") or "%" in host or "\\" in value or parts.fragment:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        if "." not in host or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            raise ValueError
    except (ValueError, UnicodeError):
        raise LinkError("Use a public HTTPS address on the standard HTTPS port, without login credentials or a fragment.") from None
    return parts, host


def describe(value):
    parts, host = parse_url(value)
    path = parts.path or "/"
    clean = urllib.parse.urlunsplit(("https", host, path, "", ""))
    platform, clip_id, page_import = "direct", None, False
    if _host_is(host, "youtube.com") or host == "youtu.be":
        platform = "youtube"
        if host == "youtu.be":
            identifier = path.strip("/").split("/")[0]
        elif path == "/watch":
            identifier = urllib.parse.parse_qs(parts.query).get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})/?", path)
            identifier = match[1] if match else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identifier):
            raise LinkError("Paste a YouTube video, Shorts, or recording link.")
        clean = "https://www.youtube.com/watch?v=" + identifier
        page_import = True
    elif _host_is(host, "twitch.tv"):
        platform = "twitch"
        if host == "clips.twitch.tv":
            candidate = path.strip("/")
        else:
            match = re.fullmatch(r"/[^/]+/clip/([A-Za-z0-9_-]+)/?", path)
            candidate = match[1] if match else ""
        if candidate and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", candidate):
            clip_id = candidate
            clean = "https://clips.twitch.tv/" + clip_id
            page_import = True
        elif re.fullmatch(r"/(?:[^/]+/)?videos/\d{1,30}/?", path):
            video_id = path.strip("/").split("/")[-1]
            clean = "https://www.twitch.tv/videos/" + video_id
            page_import = True
    elif _host_is(host, "kick.com"):
        platform = "kick"
        clip = urllib.parse.parse_qs(parts.query).get("clip", [""])[0]
        if re.fullmatch(r"/[A-Za-z0-9_-]+/?", path) and re.fullmatch(r"clip_[A-Za-z0-9_-]{1,150}", clip):
            clean = "https://kick.com" + path.rstrip("/") + "/clips/" + clip
            page_import = True
        elif re.fullmatch(r"/[A-Za-z0-9_-]+/clips/clip_[A-Za-z0-9_-]{1,150}/?", path):
            page_import = True
        elif re.fullmatch(r"/[A-Za-z0-9_-]+/videos/[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}/?", path):
            page_import = True
    elif _host_is(host, "instagram.com"):
        platform = "instagram"
        if not re.fullmatch(r"/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?", path):
            raise LinkError("Paste an Instagram post or Reel link.")
    elif _host_is(host, "tiktok.com"):
        platform = "tiktok"
    elif any(_host_is(host, suffix) for suffix in PLATFORM_CDNS):
        raise LinkError("Use the platform's original video page, or an authorized original video file. Platform CDN URLs cannot be imported directly.")
    if platform != "direct" and path == "/":
        raise LinkError("Paste a specific video or clip link, rather than the platform home page.")
    if platform == "direct" and Path(urllib.parse.unquote(path)).suffix.lower() not in VIDEO_EXTENSIONS:
        raise LinkError("For a direct download, use an HTTPS video-file link such as .mp4 or .mov. Supported platform post links can also be saved as references.")
    downloadable = platform == "direct" or page_import
    message = ("Direct video link. Confirm you can download and edit this media." if platform == "direct" else
               "The local link importer can download this recording onto your computer. Availability depends on the video platform." if page_import else
               "Link saved as a reference. Attach an authorized video file to edit it; this platform page is not downloaded.")
    return {"platform": platform, "url": clean, "clip_id": clip_id, "page_import": page_import, "can_download": downloadable, "message": message}


def _credentials(settings):
    try:
        path = settings.resolve(settings.twitch.credentials_file)
        if path.stat().st_size > 65536:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("client_id", "access_token", "editor_id", "broadcaster_id"):
            if not isinstance(data.get(key), str) or not data[key] or len(data[key]) > 4096 or any(ord(c) < 33 for c in data[key]):
                raise ValueError
        if not all(re.fullmatch(r"\d{1,30}", data[k]) for k in ("editor_id", "broadcaster_id")):
            raise ValueError
        if data.get("expires_at"):
            expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                raise ValueError
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        raise LinkError("Configure a current broadcaster/editor Twitch credential JSON file in Settings. See the link import guide.") from None


def twitch_status(settings):
    try:
        _credentials(settings)
        return {"ok": True, "message": "Twitch credential file is configured. Each import validates authorization with Twitch."}
    except LinkError as exc:
        return {"ok": False, "message": str(exc)}


def twitch_download(settings, clip_id):
    data = _credentials(settings)
    with requests.Session() as session:
        session.trust_env = False

        def get(url, params=None, api=False):
            headers = {"Authorization": ("Bearer " if api else "OAuth ") + data["access_token"]}
            if api:
                headers["Client-Id"] = data["client_id"]
            try:
                response = session.get(url, params=params, headers=headers, timeout=settings.twitch.request_timeout_s, allow_redirects=False)
                if response.status_code != 200:
                    raise LinkError("Twitch could not authorize this clip. Check the token, scopes, broadcaster and editor access.")
                return response.json()
            except (requests.RequestException, ValueError):
                raise LinkError("Twitch access could not be verified. Check the connection and authorization, then try explicitly again.") from None

        token = get("https://id.twitch.tv/oauth2/validate")
        scopes = set(token.get("scopes", []))
        if token.get("client_id") != data["client_id"] or token.get("user_id") != data["editor_id"] or not scopes.intersection({"editor:manage:clips", "channel:manage:clips"}):
            raise LinkError("The Twitch token must belong to the configured editor and app, with clip management permission.")
        clips = get("https://api.twitch.tv/helix/clips", {"id": clip_id}, True).get("data", [])
        if len(clips) != 1 or clips[0].get("id") != clip_id or clips[0].get("broadcaster_id") != data["broadcaster_id"]:
            raise LinkError("This clip is unavailable or belongs to a different Twitch broadcaster.")
        result = get("https://api.twitch.tv/helix/clips/downloads", {"broadcaster_id": data["broadcaster_id"], "editor_id": data["editor_id"], "clip_id": clip_id}, True).get("data", [])
        match = next((x for x in result if x.get("clip_id") == clip_id), None)
        url = match.get("landscape_download_url") or match.get("portrait_download_url") if match else None
        if not url:
            raise LinkError("Twitch did not provide an authorized download for this clip.")
        _, host = parse_url(url)
        if not _host_is(host, "twitchcdn.net"):
            raise LinkError("Twitch returned an unsupported media host. No file was downloaded.")
        title = clips[0].get("title") or "Twitch clip"
        return url, str(title)[:200]


def _public_addresses(host):
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError
        return addresses
    except (OSError, ValueError):
        raise LinkError("The video host must resolve only to public internet addresses.") from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, timeout=30, context=ssl.create_default_context())
        self.pinned_ip = ip

    def connect(self):
        raw = socket.create_connection((self.pinned_ip, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def download_video(url, destination, *, twitch=False, cancelled=lambda: False, progress=lambda amount: None):
    """Download to a private staging file, with no cookies, proxies or API tokens."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".part")
    started = time.monotonic()
    try:
        for redirect in range(4):
            if cancelled():
                raise LinkError("The link import was stopped. You can retry it explicitly.")
            parts, host = parse_url(url)
            if twitch:
                if not _host_is(host, "twitchcdn.net"):
                    raise LinkError("The Twitch clip redirected to an unsupported host.")
            else:
                # Platform pages/CDNs may not be smuggled through redirects.
                if any(_host_is(host, suffix) for suffix in (*PLATFORM_CDNS, "youtube.com", "youtu.be", "twitch.tv", "kick.com", "instagram.com", "tiktok.com")):
                    raise LinkError("This redirect points to a platform service. Use its supported import option instead.")
            addresses = _public_addresses(host)
            connection = _PinnedHTTPSConnection(host, addresses[0])
            try:
                target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
                connection.request("GET", target, headers={"Accept": "video/*, application/octet-stream", "Accept-Encoding": "identity", "User-Agent": "ClipForge/0.3"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or redirect == 3:
                        raise LinkError("The video link has too many redirects or no destination.")
                    url = urllib.parse.urljoin(url, location)
                    continue
                if response.status != 200:
                    raise LinkError("The video server did not provide the file. Check that the link is current and permits downloading.")
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                if not content_type.startswith("video/") and content_type != "application/octet-stream":
                    raise LinkError("This link returned a webpage or unsupported content instead of a video file.")
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding != "identity":
                    raise LinkError("The server returned unsupported compressed content.")
                length = response.getheader("Content-Length")
                if length is not None:
                    if not re.fullmatch(r"\d{1,20}", length) or not 0 < int(length) <= MAX_BYTES:
                        raise LinkError("The remote file is empty or exceeds the 10 GiB import limit.")
                    if shutil.disk_usage(destination.parent).free < int(length) + 100 * 1024**2:
                        raise LinkError("There is not enough free disk space for this video.")
                total = 0
                with staging.open("xb") as handle:
                    while True:
                        if cancelled() or time.monotonic() - started > MAX_SECONDS:
                            raise LinkError("The link import was stopped or exceeded 30 minutes. Retry with a shorter authorized recording.")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise LinkError("This video exceeds the 10 GiB import limit.")
                        handle.write(chunk)
                        progress(total)
                if not total or (length is not None and total != int(length)):
                    raise LinkError("The video transfer ended before the complete file arrived.")
                os.replace(staging, destination)
                return destination
            finally:
                connection.close()
        raise LinkError("The video redirect could not be resolved.")
    except LinkError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise LinkError("The video could not be downloaded securely. Check its link, connection and available disk space.") from None
    finally:
        staging.unlink(missing_ok=True)
