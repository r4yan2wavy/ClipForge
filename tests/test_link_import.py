"""Offline link-security regressions. Every network boundary is mocked."""
import io
import json
from types import SimpleNamespace

import pytest
import requests

from clipforge.common import Settings
from clipforge import link_import as links


@pytest.fixture(autouse=True)
def forbid_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("This test must not contact a network service")
    monkeypatch.setattr(links.socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(links.socket, "create_connection", forbidden)
    monkeypatch.setattr(links.requests, "Session", forbidden)


@pytest.mark.parametrize("url", [
    "http://media.example.com/clip.mp4",
    "file:///C:/secret.mp4",
    "https://localhost/clip.mp4",
    "https://127.0.0.1/clip.mp4",
    "https://10.1.2.3/clip.mp4",
    "https://172.16.0.1/clip.mp4",
    "https://192.168.1.1/clip.mp4",
    "https://169.254.169.254/clip.mp4",
    "https://[::1]/clip.mp4",
    "https://[::ffff:127.0.0.1]/clip.mp4",
    "https://[::ffff:10.1.2.3]/clip.mp4",
    "https://user:password@media.example.com/clip.mp4",
    "https://media.example.com:8443/clip.mp4",
    "https://media.example.com/clip.mp4#fragment",
    "https://media.example.com./clip.mp4",
    "https://media.local/clip.mp4",
    "https://media.example.com/clip.mp4\n",
    "https://media.example.com\\@127.0.0.1/clip.mp4",
])
def test_nonpublic_or_ambiguous_urls_are_rejected(url):
    with pytest.raises(links.LinkError):
        links.parse_url(url)


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["::ffff:127.0.0.1"], ["169.254.169.254"]])
def test_dns_must_resolve_only_to_public_addresses(monkeypatch, addresses):
    monkeypatch.setattr(links.socket, "getaddrinfo", lambda *a, **k: [
        (None, None, None, None, (address, 443)) for address in addresses
    ])
    with pytest.raises(links.LinkError, match="public internet"):
        links._public_addresses("media.example.com")


def test_tls_connects_to_checked_ip_with_original_host_sni(monkeypatch):
    calls = []
    raw = SimpleNamespace(close=lambda: calls.append("raw_closed"))
    wrapped = object()
    context = SimpleNamespace(wrap_socket=lambda sock, *, server_hostname: calls.append((sock, server_hostname)) or wrapped)
    monkeypatch.setattr(links.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(links.socket, "create_connection", lambda address, timeout: calls.append((address, timeout)) or raw)
    connection = links._PinnedHTTPSConnection("media.example.com", "8.8.8.8")
    connection.connect()
    assert calls == [(("8.8.8.8", 443), 30), (raw, "media.example.com")]
    assert connection.sock is wrapped


def test_reference_classification_and_signed_link_redaction():
    direct = links.describe("https://media.example.com/original.mp4?signature=PRIVATE")
    assert direct["can_download"] is True
    assert direct["url"] == "https://media.example.com/original.mp4"
    youtube = links.describe("https://youtu.be/abcdefghijk?tracking=PRIVATE")
    assert youtube["platform"] == "youtube" and youtube["can_download"] is False
    assert youtube["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert links.describe("https://www.twitch.tv/owner/videos/123")["can_download"] is False
    assert links.describe("https://clips.twitch.tv/ARealClipSlug")["clip_id"] == "ARealClipSlug"


@pytest.mark.parametrize("host", ["r1.googlevideo.com", "video.fbcdn.net", "media.twitchcdn.net", "video.tiktokcdn.com"])
def test_platform_cdn_is_not_a_direct_download_bypass(host):
    with pytest.raises(links.LinkError, match="CDN"):
        links.describe(f"https://{host}/clip.mp4")


class Response:
    def __init__(self, body=b"video", *, status=200, headers=None, read_error=False):
        self.status = status
        self.headers = {"Content-Type": "video/mp4", **(headers or {})}
        self.body = io.BytesIO(body)
        self.read_error = read_error
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, amount):
        self.reads += 1
        if self.read_error and self.reads > 1:
            raise OSError("An untrusted remote error containing PRIVATE")
        return self.body.read(amount)


def wire(monkeypatch, responses, *, dns=None):
    outcomes = iter(responses)
    records = []

    class Connection:
        def __init__(self, host, ip):
            self.host, self.ip = host, ip
            self.response = next(outcomes)
            self.closed = False
            self.calls = []
            records.append(self)

        def request(self, method, target, headers):
            self.calls.append((method, target, headers))

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr(links, "_PinnedHTTPSConnection", Connection)
    if dns is None:
        monkeypatch.setattr(links, "_public_addresses", lambda host: ["8.8.8.8"])
    else:
        monkeypatch.setattr(links.socket, "getaddrinfo", dns)
    return records


def test_redirects_use_new_checked_destinations_and_no_credentials(monkeypatch, tmp_path):
    dns_calls = []
    def dns(host, *args, **kwargs):
        dns_calls.append(host)
        address = "8.8.8.8" if host == "media.example.com" else "1.1.1.1"
        return [(None, None, None, None, (address, 443))]
    connections = wire(monkeypatch, [
        Response(status=302, headers={"Location": "https://files.example.com/video.mp4?signature=PRIVATE"}),
        Response(b"actual-bytes", headers={"Content-Length": "12"}),
    ], dns=dns)
    target = tmp_path / "result.mp4"
    progress = []
    assert links.download_video("https://media.example.com/clip.mp4", target, progress=progress.append) == target
    assert target.read_bytes() == b"actual-bytes"
    assert progress == [12]
    assert dns_calls == ["media.example.com", "files.example.com"]
    assert [(c.host, c.ip) for c in connections] == [("media.example.com", "8.8.8.8"), ("files.example.com", "1.1.1.1")]
    assert all(c.closed for c in connections)
    headers = connections[1].calls[0][2]
    assert not {key.lower() for key in headers} & {"authorization", "cookie", "client-id", "proxy-authorization"}
    assert connections[1].calls[0][1] == "/video.mp4?signature=PRIVATE"
    assert not target.with_suffix(".mp4.part").exists()


@pytest.mark.parametrize("destination", [
    "https://127.0.0.1/private.mp4",
    "http://files.example.com/video.mp4",
    "https://files.example.com:8443/video.mp4",
    "https://r1.googlevideo.com/video.mp4",
    "https://video.tiktokcdn.com/video.mp4",
    "https://www.instagram.com/reel/secret",
])
def test_unsafe_redirect_is_rejected_before_connecting(monkeypatch, tmp_path, destination):
    connections = wire(monkeypatch, [Response(status=302, headers={"Location": destination})])
    target = tmp_path / "result.mp4"
    with pytest.raises(links.LinkError):
        links.download_video("https://media.example.com/clip.mp4", target)
    assert len(connections) == 1 and connections[0].closed
    assert not target.exists() and not target.with_suffix(".mp4.part").exists()


def test_redirect_dns_cannot_rebind_into_a_private_network(monkeypatch, tmp_path):
    def dns(host, *args, **kwargs):
        return [(None, None, None, None, ("8.8.8.8" if host == "media.example.com" else "10.0.0.1", 443))]
    connections = wire(monkeypatch, [Response(status=302, headers={"Location": "https://other.example.com/clip.mp4"})], dns=dns)
    with pytest.raises(links.LinkError, match="public internet"):
        links.download_video("https://media.example.com/clip.mp4", tmp_path / "result.mp4")
    assert len(connections) == 1 and connections[0].closed


@pytest.mark.parametrize("headers", [
    {"Content-Type": "text/html"},
    {"Content-Encoding": "gzip"},
    {"Content-Length": "0"},
    {"Content-Length": "not-a-number"},
    {"Content-Length": "9999999999999999999999"},
    {"Content-Length": str(links.MAX_BYTES + 1)},
])
def test_invalid_remote_headers_never_leave_a_file(monkeypatch, tmp_path, headers):
    connection = wire(monkeypatch, [Response(headers=headers)])
    target = tmp_path / "result.mp4"
    with pytest.raises(links.LinkError):
        links.download_video("https://media.example.com/clip.mp4", target)
    assert connection[0].closed
    assert not target.exists() and not target.with_suffix(".mp4.part").exists()


@pytest.mark.parametrize("response", [
    Response(b"", headers={"Content-Length": "3"}),
    Response(b"abc", headers={"Content-Length": "5"}),
    Response(b"abc", read_error=True),
])
def test_incomplete_transfers_remove_partial_bytes_and_redact_remote_errors(monkeypatch, tmp_path, response):
    wire(monkeypatch, [response])
    target = tmp_path / "result.mp4"
    with pytest.raises(links.LinkError) as error:
        links.download_video("https://media.example.com/clip.mp4?signature=PRIVATE", target)
    assert "PRIVATE" not in str(error.value)
    assert not target.exists() and not target.with_suffix(".mp4.part").exists()


def test_streamed_size_is_enforced_without_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(links, "MAX_BYTES", 4)
    wire(monkeypatch, [Response(b"oversized")])
    target = tmp_path / "result.mp4"
    with pytest.raises(links.LinkError, match="limit"):
        links.download_video("https://media.example.com/clip.mp4", target)
    assert not target.exists() and not target.with_suffix(".mp4.part").exists()


def test_cancellation_after_partial_transfer_removes_staged_file(monkeypatch, tmp_path):
    wire(monkeypatch, [Response(b"abc")])
    states = iter([False, False, True])
    progress = []
    target = tmp_path / "result.mp4"
    with pytest.raises(links.LinkError, match="stopped"):
        links.download_video("https://media.example.com/clip.mp4", target, cancelled=lambda: next(states), progress=progress.append)
    assert progress == [3]
    assert not target.exists() and not target.with_suffix(".mp4.part").exists()


def test_elapsed_import_time_is_bounded(monkeypatch, tmp_path):
    wire(monkeypatch, [Response(b"abc")])
    clock = iter([0, links.MAX_SECONDS + 1])
    monkeypatch.setattr(links.time, "monotonic", lambda: next(clock))
    target = tmp_path / "result.mp4"
    with pytest.raises(links.LinkError, match="30 minutes"):
        links.download_video("https://media.example.com/clip.mp4", target)
    assert not target.exists() and not target.with_suffix(".mp4.part").exists()


class APIResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class APISession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def twitch_credentials(tmp_path):
    settings = Settings()
    settings.paths.root = str(tmp_path)
    settings.twitch.credentials_file = "twitch.json"
    data = {"client_id": "client", "access_token": "PRIVATE_AUTH_TOKEN", "editor_id": "42", "broadcaster_id": "99"}
    (tmp_path / "twitch.json").write_text(json.dumps(data), encoding="utf-8")
    return settings


def token_info(**updates):
    return {"client_id": "client", "user_id": "42", "scopes": ["editor:manage:clips"], **updates}


@pytest.mark.parametrize("updates", [
    {"client_id": "wrong-app"}, {"user_id": "wrong-user"}, {"scopes": ["clips:edit"]}, {"scopes": []},
])
def test_twitch_validates_token_owner_app_and_scope_before_metadata(monkeypatch, twitch_credentials, updates):
    session = APISession([APIResponse(token_info(**updates))])
    monkeypatch.setattr(links.requests, "Session", lambda: session)
    with pytest.raises(links.LinkError):
        links.twitch_download(twitch_credentials, "SomeClip")
    assert len(session.calls) == 1
    assert session.trust_env is False


def test_twitch_refuses_another_broadcasters_clip(monkeypatch, twitch_credentials):
    session = APISession([APIResponse(token_info()), APIResponse({"data": [{"id": "SomeClip", "broadcaster_id": "other"}]})])
    monkeypatch.setattr(links.requests, "Session", lambda: session)
    with pytest.raises(links.LinkError, match="different Twitch broadcaster"):
        links.twitch_download(twitch_credentials, "SomeClip")
    assert len(session.calls) == 2


def test_twitch_authorized_download_never_sends_api_token_to_media(monkeypatch, twitch_credentials, tmp_path):
    media_url = "https://production.assets.clips.twitchcdn.net/clip.mp4?download=PRIVATE_DOWNLOAD_SIGNATURE"
    session = APISession([
        APIResponse(token_info()),
        APIResponse({"data": [{"id": "SomeClip", "broadcaster_id": "99", "title": "My recording"}]}),
        APIResponse({"data": [{"clip_id": "SomeClip", "landscape_download_url": media_url}]}),
    ])
    monkeypatch.setattr(links.requests, "Session", lambda: session)
    url, title = links.twitch_download(twitch_credentials, "SomeClip")
    assert (url, title) == (media_url, "My recording")
    assert session.trust_env is False
    assert session.calls[0][0] == "https://id.twitch.tv/oauth2/validate"
    for endpoint, options in session.calls:
        assert options["allow_redirects"] is False
        assert "PRIVATE_AUTH_TOKEN" in options["headers"]["Authorization"]
        assert endpoint.startswith(("https://id.twitch.tv/", "https://api.twitch.tv/"))
    connections = wire(monkeypatch, [Response(b"media")])
    links.download_video(url, tmp_path / "twitch.mp4", twitch=True)
    headers = connections[0].calls[0][2]
    assert "Authorization" not in headers and "Client-Id" not in headers and "Cookie" not in headers
    assert "PRIVATE_AUTH_TOKEN" not in repr(connections[0].calls)


def test_twitch_media_redirect_cannot_leave_approved_cdn(monkeypatch, tmp_path):
    connections = wire(monkeypatch, [Response(status=302, headers={"Location": "https://files.example.com/clip.mp4"})])
    with pytest.raises(links.LinkError, match="unsupported host"):
        links.download_video("https://production.assets.clips.twitchcdn.net/clip.mp4", tmp_path / "clip.mp4", twitch=True)
    assert len(connections) == 1


def test_twitch_http_failure_redacts_credential_bearing_error(monkeypatch, twitch_credentials):
    session = APISession([requests.ConnectionError("PRIVATE_AUTH_TOKEN in a URL or diagnostic")])
    monkeypatch.setattr(links.requests, "Session", lambda: session)
    with pytest.raises(links.LinkError) as error:
        links.twitch_download(twitch_credentials, "SomeClip")
    assert "PRIVATE_AUTH_TOKEN" not in str(error.value)
