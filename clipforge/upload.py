"""Explicitly enabled OAuth uploads with durable, conservative resumable recovery.

Never print HTTP exceptions: they can contain the secret resumable session URL.
The CLI's machine-wide lock must surround calls that mutate publications.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

if TYPE_CHECKING:
    from .common import DB, Settings

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
API = "https://www.googleapis.com/youtube/v3"
INSERT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
TERMINAL = {"needs_reconciliation", "permanent_error", "needs_review"}
TRANSIENT = {429, 500, 502, 503, 504}


class UploadError(RuntimeError):
    """A safe error code, deliberately excluding remote messages and URLs."""

    def __init__(self, state: str, code: str, retry_after: datetime | None = None):
        super().__init__(code)
        self.state = state
        self.code = code
        self.retry_after = retry_after


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _token_marker(settings: Settings) -> str:
    try:
        stat = settings.resolve(settings.upload.token_file).stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return "missing"


def _next_quota_reset() -> datetime:
    local = _now().astimezone(ZoneInfo("America/Los_Angeles"))
    return (local.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).astimezone(timezone.utc)


def validate_schedule(value: str | None, min_lead_minutes: int = 60,
                      now: datetime | None = None) -> str | None:
    """Reject naive/stale ISO timestamps; save an explicit UTC instant.

    Supply an offset in the manifest, e.g. -04:00 or -05:00 for New York.
    Never attach a timezone to a naive clock time: DST can make it ambiguous.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("schedule must be an ISO timestamp with a timezone")
    try:
        scheduled = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO publication schedule") from exc
    if scheduled.tzinfo is None or scheduled.utcoffset() is None:
        raise ValueError("publication schedule must include a timezone offset")
    scheduled = scheduled.astimezone(timezone.utc)
    if scheduled <= (now or _now()) + timedelta(minutes=min_lead_minutes):
        raise ValueError("publication schedule is stale or lacks processing lead time")
    return scheduled.isoformat().replace("+00:00", "Z")


def _write_token(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # POSIX permissions are restrictive from creation. Windows also requires a
    # user-only directory ACL; chmod is not a substitute for an NTFS ACL.
    descriptor, name = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _credentials(settings: Settings):
    from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = settings.resolve(settings.upload.token_file)
    if not path.is_file():
        raise UploadError("auth_required", "authorize_first")
    try:
        credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not credentials.has_scopes(SCOPES):
            raise UploadError("auth_required", "required_scopes_missing")
        if not credentials.valid:
            if not credentials.refresh_token:
                raise UploadError("auth_required", "refresh_token_missing")
            credentials.refresh(Request())
            _write_token(path, credentials.to_json())
        return credentials
    except TransportError:
        raise UploadError("retry_wait", "token_refresh_network_failure") from None
    except RefreshError as exc:
        if getattr(exc, "retryable", False):
            raise UploadError("retry_wait", "token_refresh_transient_failure") from None
        raise UploadError("auth_required", "refresh_token_rejected") from None
    except (GoogleAuthError, ValueError, OSError):
        raise UploadError("auth_required", "token_refresh_failed_reauthorize") from None


def _session(settings: Settings):
    from google.auth.transport.requests import AuthorizedSession

    # Avoid automatic replay of a non-idempotent insert on an HTTP 401.
    return AuthorizedSession(_credentials(settings), refresh_status_codes=(),
                             max_refresh_attempts=0)


def authorize(settings: Settings) -> dict[str, Any]:
    """One-time supported installed-app browser consent, never browser automation."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not settings.upload.target_channel_id:
        raise ValueError("set upload.target_channel_id before authorization")
    client = settings.resolve(settings.upload.client_secrets)
    if not client.is_file():
        raise ValueError("create a Desktop OAuth client and install its client secrets file")
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES,
                                                    autogenerate_code_verifier=True)
    credentials = flow.run_local_server(host="127.0.0.1", port=0,
                                        access_type="offline", prompt="consent")
    # Verify the consent selected the intended channel before persisting a token.
    from google.auth.transport.requests import AuthorizedSession
    with AuthorizedSession(credentials, refresh_status_codes=(), max_refresh_attempts=0) as session:
        _verify_channel(settings, session)
    if not credentials.refresh_token:
        raise UploadError("auth_required", "offline_refresh_token_missing")
    _write_token(settings.resolve(settings.upload.token_file), credentials.to_json())
    return {"state": "authorized", "channel_id": settings.upload.target_channel_id}


def _failure(response) -> UploadError:
    reasons: set[str] = set()
    try:
        body = response.json()
        reasons = {str(item.get("reason", "")) for item in
                   body.get("error", {}).get("errors", [])}
    except (ValueError, TypeError, AttributeError):
        pass
    if reasons & {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}:
        return UploadError("quota_wait", "quota_or_upload_limit")
    if response.status_code == 401 or reasons & {"authError", "insufficientPermissions"}:
        return UploadError("auth_required", "authentication_or_scope_failure")
    if response.status_code in TRANSIENT or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return UploadError("retry_wait", f"http_{response.status_code}", _retry_after(response))
    return UploadError("permanent_error", f"http_{response.status_code}")


def _retry_after(response) -> datetime | None:
    value = response.headers.get("Retry-After") if response is not None else None
    if value:
        try:
            return _now() + timedelta(seconds=max(0, float(value)))
        except (ValueError, OverflowError):
            try:
                return parsedate_to_datetime(value).astimezone(timezone.utc)
            except (ValueError, TypeError, OverflowError):
                pass
    return None


def _pause(attempt: int, response=None) -> None:
    delay = min(2 ** attempt, 30)
    retry_after = _retry_after(response)
    if retry_after:
        delay = max(delay, (retry_after - _now()).total_seconds())
    if delay > 60:
        raise UploadError("retry_wait", "server_requested_cooldown", retry_after)
    time.sleep(delay)


def _request(session, *args, **kwargs):
    try:
        return session.request(*args, **kwargs)
    except Exception as exc:
        # Auth libraries can fail while refreshing immediately before a request.
        # Avoid importing optional Google packages on the local preparation path.
        if type(exc).__module__.startswith(("google.auth.", "google.oauth2.")):
            if type(exc).__name__ == "RefreshError":
                if getattr(exc, "retryable", False):
                    raise UploadError("retry_wait", "token_refresh_transient_failure") from None
                raise UploadError("auth_required", "refresh_token_rejected") from None
            if type(exc).__name__ == "TransportError":
                raise UploadError("retry_wait", "token_refresh_network_failure") from None
            raise UploadError("auth_required", "token_refresh_failed_reauthorize") from None
        raise


def _safe_request(settings: Settings, session, method: str, url: str, **kwargs):
    """Finite retries only for safe GETs and upload status probes."""
    for attempt in range(settings.upload.max_retries + 1):
        try:
            response = _request(session, method, url, timeout=settings.upload.request_timeout_s,
                                allow_redirects=False, **kwargs)
        except requests.RequestException:
            if attempt == settings.upload.max_retries:
                raise UploadError("retry_wait", "network_retry_exhausted") from None
            _pause(attempt)
            continue
        if response.status_code in TRANSIENT:
            failure = _failure(response)
            if failure.state != "retry_wait" or attempt == settings.upload.max_retries:
                raise failure
            _pause(attempt, response)
            continue
        return response
    raise UploadError("retry_wait", "retry_exhausted")


def _verify_channel(settings: Settings, session) -> None:
    response = _safe_request(settings, session, "GET", f"{API}/channels",
                             params={"part": "id", "mine": "true"})
    if response.status_code != 200:
        raise _failure(response)
    try:
        ids = {item["id"] for item in response.json().get("items", [])}
    except (ValueError, TypeError, KeyError):
        raise UploadError("auth_required", "invalid_channel_response") from None
    if ids != {settings.upload.target_channel_id}:
        raise UploadError("auth_required", "target_channel_mismatch")


def _schema(db: DB) -> None:
    db.conn.execute("PRAGMA secure_delete=ON")
    db.conn.execute("""CREATE TABLE IF NOT EXISTS publications (
        clip_id TEXT NOT NULL, channel_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL, render_sha256 TEXT NOT NULL,
        artifact_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
        video_path TEXT NOT NULL, total_bytes INTEGER NOT NULL,
        publish_at TEXT, session_uri TEXT, offset_bytes INTEGER NOT NULL DEFAULT 0,
        video_id TEXT, state TEXT NOT NULL,
        processing_state TEXT NOT NULL DEFAULT 'not_started',
        publication_state TEXT NOT NULL DEFAULT 'not_started',
        error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY (clip_id, channel_id))""")
    columns = {item[1] for item in db.conn.execute("PRAGMA table_info(publications)")}
    for name, definition in {
        "next_attempt_at": "TEXT", "failure_count": "INTEGER NOT NULL DEFAULT 0",
        "auth_generation": "TEXT", "last_poll_at": "TEXT", "api_refreshed_at": "TEXT",
    }.items():
        if name not in columns:
            db.conn.execute(f"ALTER TABLE publications ADD COLUMN {name} {definition}")
            if name == "api_refreshed_at":
                db.conn.execute("""UPDATE publications SET api_refreshed_at=updated_at
                    WHERE video_id IS NOT NULL OR session_uri IS NOT NULL""")
    db.conn.execute("""CREATE TABLE IF NOT EXISTS publication_tombstones (
        intent_hash TEXT PRIMARY KEY)""")
    db.conn.commit()


def _row(db: DB, clip_id: str, channel_id: str) -> dict[str, Any]:
    return dict(db.conn.execute("SELECT * FROM publications WHERE clip_id=? AND channel_id=?",
                                (clip_id, channel_id)).fetchone())


def _update(db: DB, row: dict[str, Any], **changes) -> dict[str, Any]:
    allowed = {"session_uri", "offset_bytes", "video_id", "state", "processing_state",
               "publication_state", "error", "next_attempt_at", "failure_count",
               "auth_generation", "last_poll_at", "api_refreshed_at"}
    if not set(changes) <= allowed:
        raise ValueError("unsupported publication update")
    changes["updated_at"] = _now().isoformat()
    columns = ",".join(f"{column}=?" for column in changes)
    with db.transaction():
        db.conn.execute(f"UPDATE publications SET {columns} WHERE clip_id=? AND channel_id=?",
                        (*changes.values(), row["clip_id"], row["channel_id"]))
    return _row(db, row["clip_id"], row["channel_id"])


def _public(row: dict[str, Any]) -> dict[str, Any]:
    # Explicit allowlist: never accidentally expose session_uri or OAuth data.
    return {key: row.get(key) for key in ("clip_id", "channel_id", "state", "video_id",
                                         "offset_bytes", "processing_state",
                                         "publication_state", "publish_at", "error", "next_attempt_at")}


def _held(settings: Settings, row: dict[str, Any]) -> bool:
    if row["state"] == "auth_required":
        return row["auth_generation"] == _token_marker(settings)
    return bool(row["next_attempt_at"] and _date(row["next_attempt_at"]) > _now())


def _save_failure(settings: Settings, db: DB, row: dict[str, Any],
                  error: UploadError) -> dict[str, Any]:
    count = row["failure_count"]
    state, code, next_attempt = error.state, error.code, None
    if state == "quota_wait":
        next_attempt = _next_quota_reset()
    elif state == "retry_wait":
        count += 1
        if count >= settings.upload.max_retries + 1:
            state, code = "needs_review", "cross_cycle_retries_exhausted"
        else:
            next_attempt = max(_now() + timedelta(seconds=min(60 * 2 ** (count - 1), 3600)),
                               error.retry_after or _now())
    return _update(db, row, state=state, error=code, failure_count=count,
                   auth_generation=_token_marker(settings) if state == "auth_required" else None,
                   next_attempt_at=next_attempt.isoformat() if next_attempt else None)


def _intent_hash(channel_id: str, clip_id: str) -> str:
    return hashlib.sha256(f"{channel_id}\0{clip_id}".encode()).hexdigest()


def expire_owner_observations(db: DB) -> int:
    """Delete unrefreshed remote identifiers/status within 30 days; keep a local guard."""
    _schema(db)
    records = [dict(item) for item in db.conn.execute("SELECT * FROM publications WHERE api_refreshed_at IS NOT NULL")]
    expired = 0
    for row in records:
        if _date(row["api_refreshed_at"]) <= _now() - timedelta(days=30):
            _update(db, row, video_id=None, session_uri=None, offset_bytes=0,
                    processing_state="deleted_stale_observation", publication_state="unknown",
                    state="needs_reconciliation", error="stale_api_data_deleted",
                    api_refreshed_at=None, last_poll_at=None)
            expired += 1
    if expired:
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return expired


def delete_owner_upload_data(settings: Settings, db: DB) -> dict[str, Any]:
    """Delete local OAuth/API upload records; never delete a video on YouTube.

    Only irreversible hashes of locally configured clip/channel intentions remain,
    preventing a subsequent worker from treating deleted history as a new upload.
    Creator-supplied originals/prepared files have a separate local-file lifecycle.
    """
    _schema(db)
    channel = settings.upload.target_channel_id
    records = list(db.conn.execute("SELECT clip_id FROM publications WHERE channel_id=?", (channel,)))
    db.conn.execute("PRAGMA secure_delete=ON")
    with db.transaction():
        for row in records:
            db.conn.execute("INSERT OR IGNORE INTO publication_tombstones(intent_hash) VALUES(?)",
                            (_intent_hash(channel, row["clip_id"]),))
        db.conn.execute("DELETE FROM publications WHERE channel_id=?", (channel,))
    token = settings.resolve(settings.upload.token_file)
    if token.exists():
        token.unlink()
    # Reclaim SQLite pages/WAL that could otherwise retain deleted credentials.
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn.execute("VACUUM")
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"state": "owner_upload_data_deleted", "count": len(records),
            "youtube_videos_deleted": False}


def revoke_owner_consent(settings: Settings, db: DB) -> dict[str, Any]:
    """Explicit user-requested remote consent revocation plus local API-data deletion."""
    token_file = settings.resolve(settings.upload.token_file)
    confirmed = False
    if token_file.is_file():
        try:
            saved = json.loads(token_file.read_text(encoding="utf-8"))
            token = saved.get("refresh_token") or saved.get("token")
            if token:
                response = requests.post("https://oauth2.googleapis.com/revoke", data={"token": token},
                                         timeout=settings.upload.request_timeout_s, allow_redirects=False)
                confirmed = response.status_code == 200
        except (OSError, ValueError, requests.RequestException):
            pass
    result = delete_owner_upload_data(settings, db)
    result["revocation_confirmed"] = confirmed
    if not confirmed:
        result["action"] = "confirm_revocation_in_google_account_permissions"
    return result


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _body(settings: Settings, artifact: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if artifact.get("state") == "SUPERSEDED":
        raise ValueError("superseded artifact cannot start an upload")
    source = artifact.get("source_manifest", {})
    if source.get("editorial_approved") is not True:
        raise ValueError("editorial approval is required before upload")
    if source.get("metadata_approved") is not True:
        raise ValueError("reviewed metadata approval is required before upload")
    if (artifact.get("candidate", {}).get("needs_visual_review")
            and source.get("visual_approved") is not True):
        raise ValueError("visual approval is required for this clip")
    made_for_kids = source.get("made_for_kids")
    if made_for_kids is None:
        made_for_kids = settings.upload.made_for_kids
    synthetic = source.get("contains_synthetic_media")
    if synthetic is None:
        synthetic = settings.upload.contains_synthetic_media
    if type(made_for_kids) is not bool or type(synthetic) is not bool:
        raise ValueError("set accurate made_for_kids and contains_synthetic_media declarations")
    publish_at = validate_schedule(source.get("schedule"), settings.upload.min_lead_minutes)
    privacy = settings.upload.privacy_status
    if privacy not in {"private", "public", "unlisted"}:
        raise ValueError("unsupported explicit privacy status")
    status = {"privacyStatus": "private" if publish_at else privacy,
              "selfDeclaredMadeForKids": made_for_kids,
              "containsSyntheticMedia": synthetic}
    if publish_at:
        status["publishAt"] = publish_at
    metadata = artifact.get("metadata", {})
    title = metadata.get("title")
    description = metadata.get("description", "")
    tags = metadata.get("tags", [])
    if not isinstance(title, str) or not title.strip() or len(title) > 100 or any(c in title for c in "<>"):
        raise ValueError("title must be nonempty, at most 100 characters, and exclude angle brackets")
    if not isinstance(description, str) or len(description.encode("utf-8")) > 5000:
        raise ValueError("description must be text of at most 5000 UTF-8 bytes")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("tags must be a list of nonempty strings")
    # API counts quoted multiword tags and separators toward the total.
    if sum(len(tag) + (2 if " " in tag else 0) for tag in tags) + max(0, len(tags) - 1) > 500:
        raise ValueError("tag length exceeds conservative API budget")
    return {"snippet": {"title": title, "description": description, "tags": tags},
            "status": status}, publish_at


def _valid_session(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        return (parsed.scheme == "https" and parsed.hostname in {"www.googleapis.com", "youtube.googleapis.com"}
                and parsed.port in {None, 443} and parsed.username is None
                and parsed.path.startswith("/upload/youtube/"))
    except ValueError:
        return False


def _consume(db: DB, row: dict[str, Any], response) -> dict[str, Any]:
    if response.status_code in {200, 201}:
        try:
            video_id = response.json()["id"]
            if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise UploadError("needs_reconciliation", "completed_without_video_id") from None
        return _update(db, row, state="upload_accepted", video_id=video_id,
                       offset_bytes=row["total_bytes"], processing_state="pending",
                       publication_state="pending", error=None, failure_count=0,
                       next_attempt_at=None, api_refreshed_at=_now().isoformat())
    if response.status_code == 308:
        match = re.fullmatch(r"(?:bytes=)?0-(\d+)", response.headers.get("Range", ""))
        if not response.headers.get("Range"):
            offset = 0
        elif match:
            offset = int(match.group(1)) + 1
        else:
            raise UploadError("needs_reconciliation", "invalid_acknowledged_range")
        if offset < row["offset_bytes"] or offset > row["total_bytes"]:
            raise UploadError("needs_reconciliation", "inconsistent_acknowledged_offset")
        return _update(db, row, state="uploading", offset_bytes=offset, error=None,
                       next_attempt_at=None, api_refreshed_at=_now().isoformat())
    if response.status_code in {404, 410}:
        raise UploadError("needs_reconciliation", "session_expired_or_unavailable")
    raise _failure(response)


def _probe(settings: Settings, db: DB, row: dict[str, Any], session) -> dict[str, Any]:
    response = _safe_request(settings, session, "PUT", row["session_uri"], data=b"",
                             headers={"Content-Length": "0",
                                      "Content-Range": f"bytes */{row['total_bytes']}"})
    return _consume(db, row, response)


def _transfer(settings: Settings, db: DB, row: dict[str, Any], session,
              existing: bool) -> dict[str, Any]:
    if existing:
        row = _probe(settings, db, row, session)
    failures = 0
    with Path(row["video_path"]).open("rb") as video:
        while not row["video_id"]:
            if row["publish_at"]:
                # A retry must never silently shift the reserved publication time.
                try:
                    validate_schedule(row["publish_at"], 0)
                except ValueError:
                    raise UploadError("needs_review", "schedule_passed_during_upload") from None
            start = row["offset_bytes"]
            if start == row["total_bytes"]:
                row = _probe(settings, db, row, session)
                if row["video_id"]:
                    return row
                raise UploadError("needs_reconciliation", "all_bytes_acknowledged_without_video_id")
            video.seek(start)
            data = video.read(settings.upload.chunk_bytes)
            row = _update(db, row, state="uploading")  # Commit before sending bytes.
            try:
                response = _request(session, "PUT", row["session_uri"], data=data,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                             "Content-Range": f"bytes {start}-{start + len(data) - 1}/{row['total_bytes']}"},
                    timeout=settings.upload.request_timeout_s, allow_redirects=False)
            except requests.RequestException:
                response = None
            if response is None or response.status_code in TRANSIENT:
                failures += 1
                # Query the existing session, including when the final response
                # was lost. Never retry a new videos.insert here.
                row = _probe(settings, db, row, session)
                if row["video_id"]:
                    return row
                if failures > settings.upload.max_retries:
                    raise UploadError("retry_wait", "chunk_retry_exhausted")
                _pause(failures - 1, response)
                continue
            row = _consume(db, row, response)
            if row["offset_bytes"] <= start and not row["video_id"]:
                failures += 1
                if failures > settings.upload.max_retries:
                    raise UploadError("retry_wait", "no_upload_progress")
                _pause(failures - 1, response)
            else:
                failures = 0
    return row


def upload_prepared(settings: Settings, db: DB, artifact_dir: Path,
                    enable_upload: bool = False, *, publication_options: dict | None = None) -> dict[str, Any]:
    try:
        artifact = json.loads((Path(artifact_dir) / "artifact.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "needs_review", "error": "prepared_artifact_unavailable"}
    required_text = ("clip_id", "source_sha256", "render_sha256", "video_path")
    if (not isinstance(artifact, dict)
            or any(not isinstance(artifact.get(key), str) or not artifact[key].strip()
                   for key in required_text)
            or not isinstance(artifact.get("metadata"), dict)
            or not isinstance(artifact.get("source_manifest"), dict)
            or not isinstance(artifact.get("candidate", {}), dict)):
        return {"state": "needs_review", "error": "prepared_artifact_invalid_shape"}
    if not (enable_upload and settings.upload.enabled):
        return {"clip_id": artifact["clip_id"], "state": "prepared", "upload_enabled": False}
    if not settings.upload.target_channel_id:
        raise ValueError("upload.target_channel_id is required")
    if settings.upload.chunk_bytes <= 0 or settings.upload.chunk_bytes % (256 * 1024):
        raise ValueError("chunk_bytes must be a positive multiple of 256 KiB")
    _schema(db)
    expire_owner_observations(db)
    clip_id, channel = artifact["clip_id"], settings.upload.target_channel_id
    tombstone = db.conn.execute("SELECT 1 FROM publication_tombstones WHERE intent_hash=?",
                               (_intent_hash(channel, clip_id),)).fetchone()
    if tombstone:
        return {"clip_id": clip_id, "state": "needs_reconciliation", "error": "history_deleted_do_not_reinsert"}
    saved = db.conn.execute("SELECT * FROM publications WHERE clip_id=? AND channel_id=?",
                            (clip_id, channel)).fetchone()
    row = dict(saved) if saved else None
    if row and (row["video_id"] or row["state"] in TERMINAL):
        return _public(row)
    if row and _held(settings, row):
        return _public(row)
    video = Path(artifact["video_path"])
    if not video.is_absolute() or not video.is_file() or video.stat().st_size == 0:
        return {"clip_id": clip_id, "state": "needs_review", "error": "prepared_video_unavailable"}
    if _hash(video) != artifact["render_sha256"]:
        return {"clip_id": clip_id, "state": "needs_review", "error": "prepared_video_checksum_mismatch"}
    serialized = json.dumps(artifact, sort_keys=True, ensure_ascii=False)
    if row and serialized != row["artifact_json"]:
        return _public(_update(db, row, state="needs_reconciliation", error="artifact_changed_after_insert"))
    # A ready record has never sent an insert; an explicit rejection is also safe
    # to retry. An initiating record with no returned URI is always ambiguous.
    safe_new_session = bool(row and not row["session_uri"] and
                            (row["state"] in {"ready", "auth_required", "retry_wait", "quota_wait"}
                             or (row["error"] or "").startswith("insert_rejected_")))
    if row and not row["session_uri"] and not safe_new_session:
        return _public(_update(db, row, state="needs_reconciliation", error="insert_outcome_unknown"))
    try:
        if not row:
            body_artifact = artifact
            if publication_options is not None:
                import copy
                if set(publication_options) != {"made_for_kids", "contains_synthetic_media", "schedule"}:
                    raise ValueError("invalid_publication_options")
                body_artifact = copy.deepcopy(artifact)
                body_artifact["source_manifest"].update(publication_options)
            body, publish_at = _body(settings, body_artifact)
        elif safe_new_session:
            body = json.loads(row["metadata_json"])
            validate_schedule(row["publish_at"], settings.upload.min_lead_minutes)
    except ValueError as exc:
        return {"clip_id": clip_id, "state": "needs_review", "error": str(exc)}
    if not row:
        with db.transaction():
            db.conn.execute("""INSERT INTO publications (
                clip_id,channel_id,source_sha256,render_sha256,artifact_json,metadata_json,
                video_path,total_bytes,publish_at,state,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'ready',?)""",
                (clip_id, channel, artifact["source_sha256"], artifact["render_sha256"],
                 serialized, json.dumps(body, sort_keys=True), str(video), video.stat().st_size,
                 publish_at, _now().isoformat()))
        row = _row(db, clip_id, channel)
    try:
        with _session(settings) as session:
            _verify_channel(settings, session)
            existing = bool(row["session_uri"])
            if not existing:
                row = _update(db, row, state="initiating", error=None)
                try:
                    response = _request(session, "POST", INSERT_URL,
                        params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
                        headers={"X-Upload-Content-Length": str(row["total_bytes"]),
                                 "X-Upload-Content-Type": "video/mp4"},
                        timeout=settings.upload.request_timeout_s, allow_redirects=False)
                except requests.RequestException:
                    raise UploadError("needs_reconciliation", "insert_response_lost") from None
                if response.status_code not in {200, 201}:
                    failure = _failure(response)
                    if 400 <= response.status_code < 500:
                        failure.code = "insert_rejected_" + failure.code
                    else:
                        failure = UploadError("needs_reconciliation", "insert_outcome_unknown")
                    raise failure
                uri = response.headers.get("Location", "")
                if not _valid_session(uri):
                    raise UploadError("needs_reconciliation", "missing_or_invalid_session_uri")
                row = _update(db, row, session_uri=uri, state="uploading", error=None,
                              api_refreshed_at=_now().isoformat())
            row = _transfer(settings, db, row, session, existing)
    except UploadError as exc:
        if exc.code == "refresh_token_rejected":
            delete_owner_upload_data(settings, db)
            return {"clip_id": clip_id, "state": "auth_required", "error": "consent_data_deleted_reauthorize"}
        if row:
            row = _save_failure(settings, db, row, exc)
            return _public(row)
        return {"clip_id": clip_id, "channel_id": channel, "state": exc.state, "error": exc.code}
    return _public(row)


def poll_publications(settings: Settings, db: DB) -> list[dict[str, Any]]:
    """Owner-authorized status polling; acceptance, processing, publication differ."""
    _schema(db)
    expire_owner_observations(db)
    if not settings.upload.enabled:
        return []
    records = []
    for item in db.conn.execute("SELECT * FROM publications WHERE video_id IS NOT NULL AND channel_id=?",
                                (settings.upload.target_channel_id,)):
        row = dict(item)
        if _held(settings, row) or row["state"] in TERMINAL:
            continue
        interval = (timedelta(days=28) if row["publication_state"] in {"published", "failed", "private", "unlisted"}
                    else timedelta(minutes=settings.upload.poll_every_minutes))
        previous = row["last_poll_at"]
        if previous and _date(previous) + interval > _now():
            continue
        records.append(row)
    if not records:
        return []
    results = []
    try:
        with _session(settings) as session:
            _verify_channel(settings, session)
            for row in records:
                # Persist the poll timestamp before the network request, including
                # interrupted polls, so rapid worker restarts do not hammer APIs.
                row = _update(db, row, last_poll_at=_now().isoformat())
                try:
                    response = _safe_request(settings, session, "GET", f"{API}/videos",
                        params={"part": "snippet,status,processingDetails", "id": row["video_id"]})
                    if response.status_code != 200:
                        raise _failure(response)
                    items = response.json().get("items", [])
                except UploadError as exc:
                    results.append(_public(_save_failure(settings, db, row, exc)))
                    continue
                except (ValueError, TypeError, AttributeError):
                    results.append(_public(_update(db, row, state="needs_reconciliation",
                                                  error="invalid_poll_response")))
                    continue
                if len(items) != 1:
                    row = _update(db, row, state="needs_reconciliation", error="uploaded_video_unavailable",
                                  video_id=None, session_uri=None, offset_bytes=0,
                                  processing_state="deleted_unavailable", publication_state="unknown",
                                  api_refreshed_at=None)
                    results.append(_public(row))
                    continue
                item = items[0]
                if item.get("snippet", {}).get("channelId") != row["channel_id"]:
                    raise UploadError("auth_required", "uploaded_video_channel_mismatch")
                status = item.get("status", {})
                processing = item.get("processingDetails", {}).get("processingStatus", "unknown")
                if status.get("uploadStatus") in {"failed", "rejected", "deleted"} or processing == "failed":
                    state, publication, error = "processing_failed", "failed", "youtube_processing_or_rejection"
                elif status.get("privacyStatus") == "public" and processing == "succeeded":
                    state, publication, error = "published", "published", None
                elif processing == "succeeded":
                    overdue = row["publish_at"] and datetime.fromisoformat(row["publish_at"].replace("Z", "+00:00")) <= _now()
                    state = "publication_review" if overdue else "ready_on_youtube"
                    publication, error = ("overdue", "scheduled_video_not_public") if overdue else ("scheduled" if row["publish_at"] else status.get("privacyStatus", "unknown"), None)
                else:
                    state, publication, error = "processing", "pending", None
                row = _update(db, row, state=state, processing_state=processing,
                              publication_state=publication, error=error, failure_count=0,
                              next_attempt_at=None, api_refreshed_at=_now().isoformat())
                results.append(_public(row))
    except UploadError as exc:
        if exc.code == "refresh_token_rejected":
            delete_owner_upload_data(settings, db)
            return [{"state": "auth_required", "error": "consent_data_deleted_reauthorize"}]
        # Session/channel failures apply to all due records and persist the hold.
        for row in records:
            if not any(item.get("clip_id") == row["clip_id"] for item in results):
                results.append(_public(_save_failure(settings, db, row, exc)))
    return results
