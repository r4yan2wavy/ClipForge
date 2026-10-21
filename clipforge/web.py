"""Loopback-only browser application using the Python standard library."""
from __future__ import annotations

import json
import html
import mimetypes
import re
import secrets
import socket
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from filelock import Timeout
from pydantic import ValidationError

from .web_service import MEDIA_EXTENSIONS, WebError, WebService

MAX_UPLOAD_BYTES = 10 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
STATIC_DIR = Path(__file__).with_name("web_static")
ENTITY = r"[A-Za-z0-9_-]{1,128}"


def parse_range(value: str | None, size: int):
    """Return an inclusive single range; reject malformed and unsatisfiable ranges."""
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or size <= 0:
        raise WebError("The requested media range is unavailable.", 416)
    first, last = match.groups()
    if len(first) > 20 or len(last) > 20:
        raise WebError("The requested media range is unavailable.", 416)
    if not first:
        if not last or int(last) <= 0:
            raise WebError("The requested media range is unavailable.", 416)
        return max(0, size - int(last)), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise WebError("The requested media range is unavailable.", 416)
    return start, end


class ClipForgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, ClipForgeHandler)

    def server_close(self):
        super().server_close()
        self.service.close()

    def handle_error(self, request, client_address):
        # Disconnected browser requests should not emit bodies or URL-bearing errors.
        pass


class ClipForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ClipForge"
    sys_version = ""

    @property
    def service(self):
        return self.server.service

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        pass

    def _headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def _json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self._headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _check_origin(self, write=False):
        port = self.server.server_address[1]
        authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            authorities.update({"127.0.0.1", "localhost"})
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in authorities:
            raise WebError("Open ClipForge using its localhost address.", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {"http://" + host for host in authorities}:
            raise WebError("Requests must come from this local ClipForge page.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise WebError("Cross-site requests are not permitted.", 403)
        if write:
            token = self.headers.get("X-ClipForge-Token", "")
            if not secrets.compare_digest(token, self.service.token):
                raise WebError("This page has expired. Reload ClipForge and try again.", 403)

    def _length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise WebError("Chunked requests are not supported. Send a known file size.", 400)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"\d+", lengths[0]):
            raise WebError("A valid Content-Length is required.", 411)
        if len(lengths[0]) > 20:
            raise WebError("This request is too large.", 413)
        length = int(lengths[0])
        if length > maximum:
            raise WebError("This upload exceeds the 10 GiB limit." if maximum == MAX_UPLOAD_BYTES else "This request is too large.", 413)
        return length

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise WebError("Send this request as JSON.", 415)
        length = self._length(MAX_JSON_BYTES)
        data = self.rfile.read(length)
        if len(data) != length:
            raise WebError("The request was interrupted. Try again.")
        try:
            value = json.loads(data.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise WebError("Send a valid JSON object with finite numbers.") from None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)

    def do_OPTIONS(self):
        self._json({"error": "Cross-origin access is not available."}, 405)

    def _dispatch(self, write):
        try:
            self._check_origin(write)
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc or "\x00" in parsed.path:
                raise WebError("Invalid request path.")
            path = urllib.parse.unquote(parsed.path)
            query = urllib.parse.parse_qs(parsed.query)
            if write:
                self._post(path, query)
            else:
                self._get(path, query)
        except WebError as exc:
            # Never process trailing bytes from a rejected POST as another request.
            self.close_connection = True
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self._json({"error": "ClipForge could not finish this request. Reload the page and check your local setup."}, 500)

    def _get(self, path, query):
        if path == "/api/bootstrap":
            self._json(self.service.bootstrap())
        elif path == "/api/library":
            self._json(self.service.library())
        elif path == "/api/health":
            self._json(self.service.health())
        elif path == "/api/stories":
            self._json(self.service.stories())
        elif path == "/api/story-voices":
            self._json(self.service.story_voices(refresh=query.get("refresh") == ["1"]))
        elif match := re.fullmatch(rf"/api/stories/({ENTITY})", path):
            self._json(self.service.get_story(match[1]))
        elif match := re.fullmatch(r"/api/social/(instagram|tiktok)/creator", path):
            self._json(self.service.social_creator(match[1]))
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})", path):
            self._json(self.service.get_clip(match[1]))
        elif match := re.fullmatch(rf"/media/story-voice/({ENTITY})", path):
            media, name = self.service.story_voice_media(match[1])
            self._file(media, ranges=True)
        elif match := re.fullmatch(rf"/media/(source|clip|caption)/({ENTITY})", path):
            media, name = self.service.media(match[1], match[2])
            self._file(media, name if query.get("download") == ["1"] or match[1] == "caption" else None, ranges=True)
        elif path.startswith("/api/") or path.startswith("/media/"):
            raise WebError("This page or media item could not be found.", 404)
        elif path in {"/guide/links", "/guide/social", "/guide/stories"}:
            name = {"/guide/links": "link-import.md", "/guide/social": "social-publishing.md",
                    "/guide/stories": "story-generator.md"}[path]
            document = Path(__file__).with_name("guides") / name
            if not document.is_file():
                document = STATIC_DIR.parent.parent / "docs" / name
            if not document.is_file():
                raise WebError("This setup guide is missing. Use the full GitHub download.", 404)
            text = document.read_text(encoding="utf-8")
            page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>ClipForge setup guide</title><style>body{max-width:900px;margin:32px auto;padding:0 20px;font:16px/1.6 system-ui;color:#182334}'
                    'pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#245bc9}</style>'
                    '<a href="/">← Back to ClipForge Studio</a> · <a rel="noreferrer noopener" target="_blank" href="https://github.com/r4yan2wavy/ClipForge/blob/main/docs/'
                    + name + '">Open linked guide on GitHub</a><pre>' + html.escape(text) + '</pre></html>').encode("utf-8")
            self.send_response(200)
            self._headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(page)
        else:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            target = (STATIC_DIR / relative).resolve()
            if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
                raise WebError("This page could not be found.", 404)
            self._file(target)

    def _post(self, path, query):
        if path == "/api/sources":
            self._upload(query)
            return
        data = self._body()
        if path == "/api/stories":
            self._json(self.service.create_story(data), 201)
        elif match := re.fullmatch(r"/api/story-voices/(install|preview)", path):
            self._json(self.service.story_voice_action(match[1], data), 202)
        elif path == "/api/stories/ideas":
            self._json(self.service.story_ideas(data), 202)
        elif path == "/api/stories/batch":
            self._json(self.service.story_batch(data), 202)
        elif match := re.fullmatch(rf"/api/stories/({ENTITY})/save", path):
            self._json(self.service.save_story(match[1], data))
        elif match := re.fullmatch(rf"/api/stories/({ENTITY})/(generate|rewrite)", path):
            self._json(self.service.story_action(match[1], match[2], data), 202)
        elif match := re.fullmatch(rf"/api/stories/({ENTITY})/scenes/({ENTITY})/regenerate", path):
            self._json(self.service.story_action(match[1], "scene", data, match[2]), 202)
        elif path == "/api/import":
            if set(data) != {"path"} or not isinstance(data["path"], str) or not data["path"].strip() or len(data["path"]) > 4096:
                raise WebError("Enter the full path to an existing local video.")
            self._json(self.service.register_source(data["path"]), 201)
        elif path == "/api/links":
            self._json(self.service.import_link(data), 202)
        elif match := re.fullmatch(rf"/api/sources/({ENTITY})/delete", path):
            self._json(self.service.delete_source(match[1], data))
        elif match := re.fullmatch(rf"/api/links/({ENTITY})/(attach|retry|delete)", path):
            action = {"attach": self.service.attach_link, "retry": self.service.retry_link, "delete": self.service.delete_link}[match[2]]
            result = action(match[1], data)
            self._json(result, 202 if match[2] == "retry" else 200)
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})/social/(instagram|tiktok)/(publish|refresh|finish)", path):
            self._json(self.service.social_action(match[1], match[2], match[3], data), 202)
        elif path == "/api/demo":
            if data:
                raise WebError("The sample request does not accept options.")
            self._json(self.service.demo(), 201)
        elif path == "/api/jobs":
            self._json(self.service.enqueue_clip(data), 202)
        elif match := re.fullmatch(rf"/api/jobs/({ENTITY})/delete", path):
            self._json(self.service.delete_job(match[1], data))
        elif match := re.fullmatch(rf"/api/jobs/({ENTITY})/cancel", path):
            self._json(self.service.cancel_job(match[1], data), 202)
        elif match := re.fullmatch(rf"/api/jobs/({ENTITY})/retry", path):
            if data:
                raise WebError("Retry does not accept additional options.")
            self._json(self.service.retry(match[1]), 202)
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})/approve", path):
            self._json(self.service.approve(match[1], data))
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})/delete", path):
            self._json(self.service.delete_clip(match[1], data))
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})/publish", path):
            self._json(self.service.publish(match[1], data), 202)
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})/resume", path):
            self._json(self.service.resume(match[1], data), 202)
        elif match := re.fullmatch(rf"/api/clips/({ENTITY})", path):
            self._json(self.service.edit_clip(match[1], data))
        elif path == "/api/settings":
            self._json(self.service.update_settings(data))
        elif match := re.fullmatch(r"/api/youtube/(authorize|poll|revoke)", path):
            self._json(self.service.youtube(match[1], data), 202)
        else:
            raise WebError("This action could not be found.", 404)

    def _upload(self, query):
        if self.headers.get_content_type() != "application/octet-stream":
            raise WebError("Upload the original video as a binary file.", 415)
        length = self._length(MAX_UPLOAD_BYTES)
        name = query.get("name", [""])[0]
        # Windows and POSIX separators are both handled, without trusting paths.
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or len(name) > 240 or any(ord(c) < 32 for c in name):
            raise WebError("Choose a video with a valid filename.")
        suffix = Path(name).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS or length < 1:
            raise WebError("Choose an MP4, MOV, MKV, WebM or another supported video.")
        file_id = uuid.uuid4().hex
        folder = self.service.data_dir / "sources"
        partial = folder / (file_id + ".part")
        destination = folder / (file_id + suffix)
        try:
            with partial.open("xb") as target:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise WebError("The upload was interrupted. Choose the video and try again.")
                    target.write(chunk)
                    remaining -= len(chunk)
            partial.replace(destination)
            source = self.service.register_source(destination, name)
        except BaseException:
            partial.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
        self._json(source, 201)

    def _file(self, path, attachment=None, ranges=False):
        size = path.stat().st_size
        selected = None
        if ranges:
            try:
                selected = parse_range(self.headers.get("Range"), size)
            except WebError:
                self.send_response(416)
                self._headers()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        start, end = selected or (0, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if selected else 200)
        self._headers()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        if path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if selected:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            filename = re.sub(r"[^A-Za-z0-9._ -]", "_", attachment).strip(" .")[:160] or "clip.mp4"
            self.send_header("Content-Disposition", f"attachment; filename=\"{filename}\"; filename*=UTF-8''{urllib.parse.quote(attachment, safe='')}")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)


def create_server(config_path, port=8000, host="127.0.0.1"):
    if host != "127.0.0.1":
        raise ValueError("The studio can only listen on 127.0.0.1.")
    if not 0 <= port <= 65535:
        raise ValueError("Choose a port from 1 to 65535 (or 0 for an available test port).")
    service = WebService(config_path)
    try:
        return ClipForgeHTTPServer((host, port), service)
    except BaseException:
        service.close()
        raise


def run_server(config_path, port=8000, open_browser=True, host="127.0.0.1"):
    try:
        server = create_server(config_path, port=port, host=host)
    except WebError as exc:
        print(f"ClipForge could not start: {exc}", flush=True)
        raise SystemExit(1) from None
    except Timeout:
        print("ClipForge could not start: another command is using this workspace. Try again when it finishes.", flush=True)
        raise SystemExit(1) from None
    except OSError:
        print(f"ClipForge could not start. Port {port} may already be in use, or the workspace cannot be written. Try --port 8001 and check the config folder.", flush=True)
        raise SystemExit(1) from None
    except (ValueError, ValidationError):
        print("ClipForge could not start: check that your config file contains valid ClipForge settings.", flush=True)
        raise SystemExit(1) from None
    url = f"http://localhost:{server.server_address[1]}/"
    print(f"ClipForge Studio: {url}", flush=True)
    print("Keep this window open. Press Ctrl+C to stop the local app.", flush=True)
    if open_browser:
        threading.Timer(.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
