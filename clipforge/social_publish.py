"""Official Instagram Reels and TikTok Direct Post uploads.

Call under the engine's workspace FileLock. Credentials and upload URLs stay
private. A durable intent is committed before any remote creation/publication;
uncertain outcomes never trigger another automatic create or publish request.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from pydantic import Field, ValidationError, model_validator

from .common import DB, StrictModel, iso_now, sha256_file, stable_id

TIKTOK_API = "https://open.tiktokapis.com/v2"
INSTAGRAM_API = "https://graph.facebook.com"
PLATFORMS = {"instagram", "tiktok"}
PRIVACY = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}
CHUNK_BYTES = 10_000_000
IG_POLL_INTERVAL = 3
IG_POLL_COUNT = 6


class SocialError(ValueError):
    def __init__(self, message, code="needs_review", state="needs_review"):
        super().__init__(message)
        self.code, self.state = code, state


class InstagramOptions(StrictModel):
    confirmed: bool
    caption: str = Field(max_length=2200)
    share_to_feed: bool
    account_id: str = Field(min_length=1, max_length=64)


class TikTokOptions(StrictModel):
    confirmed: bool
    caption: str = Field(max_length=2200)
    privacy_level: str
    disable_comment: bool
    disable_duet: bool
    disable_stitch: bool
    brand_content_toggle: bool
    brand_organic_toggle: bool
    is_aigc: bool
    creator_username: str = Field(min_length=1, max_length=128)
    music_usage_confirmed: bool

    @model_validator(mode="after")
    def consent(self):
        if not self.music_usage_confirmed:
            raise ValueError("TikTok music usage consent is required")
        if self.privacy_level not in PRIVACY:
            raise ValueError("Choose a privacy option returned by TikTok")
        if self.brand_content_toggle and self.privacy_level == "SELF_ONLY":
            raise ValueError("Paid partnership content cannot use Only me visibility")
        if len(self.caption.encode("utf-16-le")) // 2 > 2200:
            raise ValueError("TikTok captions are limited to 2200 UTF-16 units")
        return self


def validate_options(platform, payload):
    if platform not in PLATFORMS:
        raise SocialError("Choose Instagram or TikTok.")
    try:
        model = (InstagramOptions if platform == "instagram" else TikTokOptions).model_validate(payload)
    except (ValidationError, UnicodeError):
        raise SocialError("Complete the account, caption, visibility and disclosure choices and confirm the platform terms.") from None
    if model.confirmed is not True:
        raise SocialError("Confirm that you want to send this reviewed video to the selected platform.")
    if "\x00" in model.caption:
        raise SocialError("The caption contains an unsupported character.")
    try:
        model.caption.encode("utf-8")
    except UnicodeError:
        raise SocialError("The caption contains unsupported Unicode characters.") from None
    return model.model_dump()


def _config(settings, platform):
    if platform not in PLATFORMS:
        raise SocialError("Choose Instagram or TikTok.")
    return getattr(settings, platform)


def _credentials(settings, platform):
    config = _config(settings, platform)
    path = settings.resolve(config.credentials_file)
    try:
        if path.stat().st_size > 64 * 1024:
            raise ValueError
        credentials = json.loads(path.read_text(encoding="utf-8"))
        token = credentials["access_token"]
        if not isinstance(token, str) or not token.strip() or len(token) > 16000 or any(c.isspace() for c in token):
            raise ValueError
        expires = credentials.get("expires_at")
        if expires is not None:
            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError
            if expiry <= datetime.now(timezone.utc):
                raise SocialError("The platform token has expired. Renew it through the provider's approved OAuth flow and replace the private credential file.", "token_expired", "auth_required")
        if platform == "instagram":
            target = str(config.target_account_id)
            if (credentials.get("login_type") != "facebook_business" or not re.fullmatch(r"\d{1,64}", target)
                    or credentials.get("account_id") != target):
                raise SocialError("Instagram needs a Facebook Login for Business Page token whose account_id matches the configured Professional account.")
            if not re.fullmatch(r"v\d{1,3}\.0", config.api_version):
                raise SocialError("Enter a supported Meta Graph API version, such as v25.0.")
        else:
            if not isinstance(credentials.get("open_id"), str) or not credentials["open_id"].strip():
                raise ValueError
            scopes = credentials.get("scope", "")
            scopes = set(scopes.replace(",", " ").split()) if isinstance(scopes, str) else set(scopes)
            if "video.publish" not in scopes:
                raise SocialError("The TikTok token must include the approved video.publish scope.", "scope_required", "auth_required")
            if type(credentials.get("client_audited", False)) is not bool:
                raise ValueError
        return credentials
    except SocialError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SocialError("Create a valid private platform credential JSON file as described in docs/social-publishing.md.", "credentials_unavailable", "auth_required") from None


def provider_status(settings, platform):
    try:
        config = _config(settings, platform)
        if not config.enabled:
            return {"ok": False, "message": "Optional publishing is disabled. Configure your approved platform credentials, then enable it."}
        _credentials(settings, platform)
        return {"ok": True, "message": "Credential file is ready. Load the account to verify access with the provider."}
    except SocialError as error:
        return {"ok": False, "message": str(error)}


def _session():
    session = requests.Session()
    session.trust_env = False
    return session


def _request(settings, platform, session, method, url, **kwargs):
    try:
        return session.request(method, url, timeout=_config(settings, platform).request_timeout_s,
                               allow_redirects=False, **kwargs)
    except requests.RequestException:
        raise SocialError("The platform request did not return a reliable response. Refresh its saved status before taking another action.", "response_unknown", "needs_reconciliation") from None


def _json_response(response, platform):
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise SocialError("The provider returned an unreadable response. Refresh status before any further action.", "invalid_response", "needs_reconciliation") from None
    error = data.get("error", {}) if isinstance(data, dict) else {}
    success = 200 <= response.status_code < 300
    if platform == "tiktok":
        success = success and isinstance(error, dict) and error.get("code") == "ok"
    else:
        success = success and not error
    if not success:
        raw_code = str(error.get("code", response.status_code)) if isinstance(error, dict) else str(response.status_code)
        code = raw_code if re.fullmatch(r"[A-Za-z0-9_]{1,100}", raw_code) else "provider_rejected"
        auth = response.status_code == 401 or code in {"190", "access_token_invalid", "scope_not_authorized"}
        state = "auth_required" if auth else ("needs_reconciliation" if response.status_code >= 500 else "rejected")
        raise SocialError("The provider rejected this request (" + code + "). Check account permissions, app approval and platform limits.", code, state)
    if not isinstance(data, dict):
        raise SocialError("The provider response was invalid.", "invalid_response", "needs_reconciliation")
    if platform == "tiktok" and not isinstance(data.get("data"), dict):
        raise SocialError("The provider response was missing its data object.", "invalid_response", "needs_reconciliation")
    return data


def _instagram_base(settings):
    return INSTAGRAM_API + "/" + settings.instagram.api_version


def creator_info(settings, platform):
    credentials = _credentials(settings, platform)
    with _session() as session:
        if platform == "instagram":
            response = _request(settings, platform, session, "GET", _instagram_base(settings) + "/me",
                headers={"Authorization": "Bearer " + credentials["access_token"]},
                params={"fields": "id,instagram_business_account{id,username}"})
            account = _json_response(response, platform).get("instagram_business_account", {})
            if (not isinstance(account, dict) or str(account.get("id", "")) != settings.instagram.target_account_id
                    or not isinstance(account.get("username"), str) or not account["username"]):
                raise SocialError("The Page token is not linked to the configured Instagram Professional account.", "account_mismatch", "auth_required")
            return {"platform": platform, "account_id": str(account["id"]), "username": account["username"][:128],
                    "nickname": account["username"][:128], "message": "Authorized Instagram Professional account. This Reel will be published to Instagram when processing finishes."}
        response = _request(settings, platform, session, "POST", TIKTOK_API + "/post/publish/creator_info/query/",
            headers={"Authorization": "Bearer " + credentials["access_token"], "Content-Type": "application/json; charset=UTF-8"}, json={})
        account = _json_response(response, platform).get("data", {})
        if (not isinstance(account, dict) or not isinstance(account.get("creator_username"), str)
                or not account["creator_username"] or not isinstance(account.get("privacy_level_options"), list)
                or type(account.get("max_video_post_duration_sec")) is not int
                or account["max_video_post_duration_sec"] < 1
                or any(type(account.get(k)) is not bool for k in ("comment_disabled", "duet_disabled", "stitch_disabled"))):
            raise SocialError("TikTok did not return valid creator publishing options. Try again later.", "creator_info_invalid")
        audited = credentials.get("client_audited", False)
        options = [p for p in account["privacy_level_options"] if p in PRIVACY and (audited or p == "SELF_ONLY")]
        return {"platform": platform, "account_id": credentials["open_id"], "username": account["creator_username"][:128],
                "nickname": str(account.get("creator_nickname") or account["creator_username"])[:128],
                "privacy_level_options": options, **{k: account[k] for k in ("comment_disabled", "duet_disabled", "stitch_disabled", "max_video_post_duration_sec")},
                "client_audited": audited, "message": "Choose the audience and interaction settings for this post." if audited else
                "Unaudited TikTok clients are restricted to private accounts and Only me posts. Provider eligibility and audit approval are still required."}


def schema(db: DB):
    db.conn.execute("""CREATE TABLE IF NOT EXISTS social_publications(
        clip_id TEXT NOT NULL, platform TEXT NOT NULL, target_account_id TEXT NOT NULL,
        artifact_hash TEXT NOT NULL, artifact_json TEXT NOT NULL, options_json TEXT NOT NULL,
        video_path TEXT NOT NULL, video_sha256 TEXT NOT NULL, total_bytes INTEGER NOT NULL,
        api_version TEXT, state TEXT NOT NULL, remote_id TEXT, upload_url TEXT,
        upload_offset INTEGER NOT NULL DEFAULT 0, remote_media_id TEXT,
        message TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        last_checked_at TEXT, PRIMARY KEY(clip_id,platform))""")
    if "publish_attempted" not in {row[1] for row in db.conn.execute("PRAGMA table_info(social_publications)")}:
        db.conn.execute("ALTER TABLE social_publications ADD COLUMN publish_attempted INTEGER NOT NULL DEFAULT 0")


def _record(db, clip_id, platform):
    row = db.conn.execute("SELECT * FROM social_publications WHERE clip_id=? AND platform=?", (clip_id, platform)).fetchone()
    return dict(row) if row else None


def _public(record):
    return {"platform": record["platform"], "clip_id": record["clip_id"], "account_id": record["target_account_id"],
            "state": record["state"], "message": record["message"] or record["state"].replace("_", " "),
            "error_code": record["error_code"], "remote_media_id": record["remote_media_id"],
            "updated_at": record["updated_at"], "can_finish": record["platform"] == "instagram" and record["state"] == "ready_to_publish" and not record.get("publish_attempted")}


def public_records(db, clip_id):
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='social_publications'").fetchone():
        return {}
    return {row["platform"]: _public(dict(row)) for row in db.conn.execute("SELECT * FROM social_publications WHERE clip_id=?", (clip_id,))}


def _update(db, record, **changes):
    allowed = {"state", "remote_id", "upload_url", "upload_offset", "remote_media_id", "message", "error_code", "last_checked_at", "publish_attempted"}
    if not changes.keys() <= allowed:
        raise ValueError("unsupported social state update")
    changes["updated_at"] = iso_now()
    assignments = ",".join(key + "=?" for key in changes)
    with db.transaction():
        db.conn.execute(f"UPDATE social_publications SET {assignments} WHERE clip_id=? AND platform=?",
                        (*changes.values(), record["clip_id"], record["platform"]))
    return _record(db, record["clip_id"], record["platform"])


def _safe_upload_url(value, platform, remote_id=None):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or not parsed.hostname):
            return False
        try:
            ipaddress.ip_address(parsed.hostname)
            return False
        except ValueError:
            pass
        if platform == "instagram":
            return parsed.hostname == "rupload.facebook.com" and parsed.path.startswith("/ig-api-upload/") and parsed.path.rstrip("/").endswith("/" + remote_id)
        return (parsed.hostname.endswith(".tiktokapis.com") and parsed.path.startswith(("/video/", "/upload/")))
    except (ValueError, TypeError, AttributeError):
        return False


def _artifact(folder):
    try:
        artifact = json.loads((Path(folder) / "artifact.json").read_text(encoding="utf-8"))
        manifest = artifact["source_manifest"]
        if (artifact.get("state") != "PREPARED" or manifest.get("editorial_approved") is not True
                or manifest.get("metadata_approved") is not True
                or (artifact["candidate"].get("needs_visual_review") and manifest.get("visual_approved") is not True)):
            raise SocialError("Watch, review and approve the clip and its metadata before publishing.")
        if artifact.get("web_is_demo"):
            raise SocialError("The synthetic setup sample cannot be published.")
        for sample in (Path(__file__).parent / "web_static" / "sample.mp4", Path(__file__).parent.parent / "demo" / "preview.mp4"):
            if sample.is_file() and artifact.get("source_sha256") == sha256_file(sample):
                raise SocialError("The synthetic setup sample cannot be published.")
        video = Path(artifact["video_path"])
        if not video.is_absolute() or not video.is_file() or not video.stat().st_size:
            raise ValueError
        if sha256_file(video) != artifact["render_sha256"]:
            raise SocialError("The rendered video changed. Reconcile the prepared artifact before publishing.")
        return artifact, video
    except SocialError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise SocialError("The prepared clip is incomplete or unavailable. Review its files before publishing.") from None


def _validate_creator(platform, info, options, artifact, video):
    duration = float(artifact["candidate"]["end_s"]) - float(artifact["candidate"]["start_s"])
    if platform == "instagram":
        if options["account_id"] != info["account_id"]:
            raise SocialError("The Instagram account changed. Load the account again and confirm the intended destination.")
        # A conservative local-file cap within the provider's Reel limits.
        if not 3 <= duration <= 900 or video.stat().st_size > 300 * 1024 * 1024:
            raise SocialError("Instagram Reels need a video of 3 seconds to 15 minutes, within this integration's 300 MiB upload limit.")
    else:
        if options["creator_username"] != info["username"]:
            raise SocialError("The TikTok account changed. Load its latest account options and confirm the destination.")
        if options["privacy_level"] not in info["privacy_level_options"]:
            raise SocialError("Choose an audience from the account's current TikTok privacy options.")
        if duration > info["max_video_post_duration_sec"] or duration < 3:
            raise SocialError("This clip is outside the duration limit returned by TikTok (at least 3 seconds).")
        if video.stat().st_size > 4 * 1024**3:
            raise SocialError("TikTok videos must fit within the 4 GiB transfer limit.")
        for setting, disabled in (("disable_comment", "comment_disabled"), ("disable_duet", "duet_disabled"), ("disable_stitch", "stitch_disabled")):
            if info[disabled] and not options[setting]:
                raise SocialError("The creator has disabled one of the requested interactions. Load the account options again.")


def publish(settings, db, folder, platform, payload):
    options = validate_options(platform, payload)
    if not _config(settings, platform).enabled:
        raise SocialError("Enable this publishing destination in Settings before posting.")
    credentials = _credentials(settings, platform)
    artifact, video = _artifact(folder)
    schema(db)
    existing = _record(db, artifact["clip_id"], platform)
    if existing:
        if (existing["artifact_hash"] != stable_id(artifact) or existing["options_json"] != json.dumps(options, sort_keys=True)
                or (platform == "instagram" and existing["target_account_id"] != settings.instagram.target_account_id)):
            raise SocialError("A publication already exists with different immutable inputs. Reconcile it instead of submitting again.")
        if platform == "instagram" and existing["state"] == "ready_to_publish":
            return finish(settings, db, folder, platform)
        return _public(existing)
    info = creator_info(settings, platform)
    _validate_creator(platform, info, options, artifact, video)
    target = info["account_id"] if platform == "instagram" else info["username"]
    with db.transaction():
        db.conn.execute("""INSERT INTO social_publications(clip_id,platform,target_account_id,artifact_hash,
            artifact_json,options_json,video_path,video_sha256,total_bytes,api_version,state,message,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,'ready','Confirmed publication intent saved.',?,?)""",
            (artifact["clip_id"], platform, target, stable_id(artifact), json.dumps(artifact, sort_keys=True, ensure_ascii=False),
             json.dumps(options, sort_keys=True), str(video), artifact["render_sha256"], video.stat().st_size,
             settings.instagram.api_version if platform == "instagram" else None, iso_now(), iso_now()))
    record = _record(db, artifact["clip_id"], platform)
    try:
        record = (_instagram_upload(settings, db, record, credentials, options) if platform == "instagram"
                  else _tiktok_upload(settings, db, record, credentials, options))
    except SocialError as error:
        record = _update(db, _record(db, artifact["clip_id"], platform), state=error.state,
                         message=str(error), error_code=error.code)
    return _public(record)


def _instagram_upload(settings, db, record, credentials, options):
    headers = {"Authorization": "Bearer " + credentials["access_token"]}
    with _session() as session:
        record = _update(db, record, state="creating", message="Creating the Instagram Reel container…")
        response = _request(settings, "instagram", session, "POST", _instagram_base(settings) + "/" + record["target_account_id"] + "/media",
            headers=headers, data={"media_type": "REELS", "upload_type": "resumable", "caption": options["caption"],
                                   "share_to_feed": str(options["share_to_feed"]).lower()})
        created = _json_response(response, "instagram")
        remote_id, uri = str(created.get("id", "")), created.get("uri")
        if not re.fullmatch(r"\d{1,128}", remote_id) or not _safe_upload_url(uri, "instagram", remote_id):
            raise SocialError("Instagram did not return a valid upload container. Reconcile this intent before trying again.", "invalid_upload_session", "needs_reconciliation")
        record = _update(db, record, state="uploading", remote_id=remote_id, upload_url=uri, message="Uploading the video file to Instagram…")
        with Path(record["video_path"]).open("rb") as stream:
            response = _request(settings, "instagram", session, "POST", uri, data=stream,
                headers={"Authorization": "OAuth " + credentials["access_token"], "offset": "0",
                         "file_size": str(record["total_bytes"]), "Content-Length": str(record["total_bytes"]), "Content-Type": "video/mp4"})
        uploaded = _json_response(response, "instagram")
        if uploaded.get("success") is not True:
            raise SocialError("Instagram did not confirm the file transfer. Refresh the existing container; do not create another post.", "upload_unconfirmed", "needs_reconciliation")
        record = _update(db, record, state="processing", upload_offset=record["total_bytes"], message="Instagram is processing the Reel. Refresh status if it takes longer.")
    for attempt in range(IG_POLL_COUNT):
        record = _instagram_status(settings, db, record, credentials)
        if record["state"] == "ready_to_publish":
            return _instagram_finish(settings, db, record, credentials)
        if record["state"] not in {"processing", "uploading"}:
            return record
        if attempt + 1 < IG_POLL_COUNT:
            time.sleep(IG_POLL_INTERVAL)
    return record


def _instagram_finish(settings, db, record, credentials):
    if record.get("publish_attempted"):
        return _update(db, record, state="needs_reconciliation", message="The publish request was already sent. Refresh the container and check Instagram instead of sending it again.")
    record = _update(db, record, state="publishing", publish_attempted=1, message="Publishing the processed Instagram Reel…")
    try:
        with _session() as session:
            response = _request(settings, "instagram", session, "POST", _instagram_base(settings) + "/" + record["target_account_id"] + "/media_publish",
                headers={"Authorization": "Bearer " + credentials["access_token"]}, data={"creation_id": record["remote_id"]})
            media_id = str(_json_response(response, "instagram").get("id", ""))
            if not re.fullmatch(r"\d{1,128}", media_id):
                raise SocialError("Instagram may have published the Reel but returned no media ID. Refresh its container status.", "publish_outcome_unknown", "needs_reconciliation")
            return _update(db, record, state="published", remote_media_id=media_id, upload_url=None,
                           error_code=None, message="Instagram confirmed the Reel was published.")
    except SocialError as error:
        # A publish POST is never replayed after a timeout or ambiguous response.
        return _update(db, record, state=error.state, message=str(error), error_code=error.code)


def _instagram_status(settings, db, record, credentials):
    if not record["remote_id"]:
        return _update(db, record, state="needs_reconciliation", message="No container ID was received. Check Instagram before any new publication.", error_code="container_outcome_unknown")
    with _session() as session:
        response = _request(settings, "instagram", session, "GET", _instagram_base(settings) + "/" + record["remote_id"],
            headers={"Authorization": "Bearer " + credentials["access_token"]}, params={"fields": "status_code"})
        status = _json_response(response, "instagram").get("status_code")
    if status == "PUBLISHED":
        return _update(db, record, state="published", upload_url=None, error_code=None, last_checked_at=iso_now(), message="Instagram confirms this container was published.")
    if status == "FINISHED":
        # A previous publish request might have succeeded before its response was
        # lost. Never turn that uncertainty into a second publish POST.
        if record.get("publish_attempted"):
            return _update(db, record, state="needs_reconciliation", last_checked_at=iso_now(), message="The publish result is uncertain. Check the Instagram account before any further action.")
        return _update(db, record, state="ready_to_publish", error_code=None, last_checked_at=iso_now(), message="The Reel is processed. Choose Finish publishing to send the saved container.")
    if status in {"ERROR", "EXPIRED"}:
        return _update(db, record, state="failed", upload_url=None, error_code="container_" + status.lower(), last_checked_at=iso_now(), message="Instagram reports that this container " + ("expired." if status == "EXPIRED" else "failed processing."))
    if status == "IN_PROGRESS":
        return _update(db, record, state="processing", error_code=None, last_checked_at=iso_now(), message="Instagram is still processing this Reel. Refresh status in a moment.")
    raise SocialError("Instagram returned an unknown container state. Check the account before posting again.", "unknown_container_status", "needs_reconciliation")


def _tiktok_upload(settings, db, record, credentials, options):
    chunk_size = min(CHUNK_BYTES, record["total_bytes"])
    count = max(1, record["total_bytes"] // chunk_size)
    post_info = {key: options[key] for key in ("privacy_level", "disable_comment", "disable_duet", "disable_stitch", "brand_content_toggle", "brand_organic_toggle", "is_aigc")}
    post_info["title"] = options["caption"]
    with _session() as session:
        record = _update(db, record, state="creating", message="Initializing the approved TikTok Direct Post…")
        response = _request(settings, "tiktok", session, "POST", TIKTOK_API + "/post/publish/video/init/",
            headers={"Authorization": "Bearer " + credentials["access_token"]}, json={"post_info": post_info,
                "source_info": {"source": "FILE_UPLOAD", "video_size": record["total_bytes"], "chunk_size": chunk_size, "total_chunk_count": count}})
        initialized = _json_response(response, "tiktok").get("data", {})
        remote_id, uri = initialized.get("publish_id"), initialized.get("upload_url")
        if not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_.~:-]{1,128}", remote_id) or not _safe_upload_url(uri, "tiktok"):
            raise SocialError("TikTok did not return a valid upload task. Reconcile this publication before trying again.", "invalid_upload_session", "needs_reconciliation")
        record = _update(db, record, state="uploading", remote_id=remote_id, upload_url=uri, message="Uploading your video to TikTok…")
        with Path(record["video_path"]).open("rb") as stream:
            for index in range(count):
                start = stream.tell()
                length = chunk_size if index < count - 1 else record["total_bytes"] - start
                data = stream.read(length)
                if len(data) != length:
                    raise SocialError("The prepared video changed during upload.", "video_changed", "needs_reconciliation")
                # This host receives only its own upload URL credentials, never
                # the API Bearer token. Redirects and environment proxies are off.
                response = _request(settings, "tiktok", session, "PUT", uri, data=data,
                    headers={"Content-Type": "video/mp4", "Content-Length": str(length),
                             "Content-Range": f"bytes {start}-{start + length - 1}/{record['total_bytes']}"})
                expected = 201 if index == count - 1 else 206
                if response.status_code != expected:
                    raise SocialError("TikTok did not confirm this upload chunk. Refresh the existing post status before any further action.", "upload_chunk_unconfirmed", "needs_reconciliation")
                record = _update(db, record, upload_offset=start + length)
        return _update(db, record, state="processing", upload_url=None, error_code=None,
                       message="TikTok accepted the file and is processing the post. Refresh status to confirm publication.")


def refresh(settings, db, clip_id, platform):
    schema(db)
    record = _record(db, clip_id, platform)
    if not record:
        raise SocialError("No saved publication exists for this clip and platform.")
    credentials = _credentials(settings, platform)
    info = creator_info(settings, platform)
    target = info["account_id"] if platform == "instagram" else info["username"]
    if target != record["target_account_id"]:
        raise SocialError("These credentials belong to a different account than the saved publication.", "account_mismatch", "auth_required")
    try:
        if platform == "instagram":
            return _public(_instagram_status(settings, db, record, credentials))
        if not record["remote_id"]:
            return _public(_update(db, record, state="needs_reconciliation", message="No TikTok publish ID was received. Check the creator account before any new post.", error_code="publish_outcome_unknown"))
        with _session() as session:
            response = _request(settings, platform, session, "POST", TIKTOK_API + "/post/publish/status/fetch/",
                headers={"Authorization": "Bearer " + credentials["access_token"]}, json={"publish_id": record["remote_id"]})
            data = _json_response(response, platform).get("data", {})
        state = data.get("status")
        if state == "PUBLISH_COMPLETE":
            ids = data.get("publicaly_available_post_id", [])
            media_id = str(ids[0]) if isinstance(ids, list) and ids and re.fullmatch(r"\d{1,64}", str(ids[0])) else None
            record = _update(db, record, state="published", remote_media_id=media_id, error_code=None, upload_url=None,
                             last_checked_at=iso_now(), message="TikTok confirms the post completed. Its visibility follows the audience you selected.")
        elif state == "FAILED":
            reason = data.get("fail_reason", "provider_processing_failed")
            reason = reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason) else "provider_processing_failed"
            record = _update(db, record, state="failed", error_code=reason, upload_url=None, last_checked_at=iso_now(), message="TikTok reports this post failed (" + reason + "). Check the account and provider requirements.")
        elif state in {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"}:
            record = _update(db, record, state="processing", last_checked_at=iso_now(), message="TikTok is still processing this post. Refresh status later.")
        else:
            record = _update(db, record, state="needs_reconciliation", last_checked_at=iso_now(), error_code="unknown_post_status", message="TikTok returned an unexpected post state. Check the creator account.")
    except SocialError as error:
        record = _update(db, record, state=error.state, message=str(error), error_code=error.code)
    return _public(record)


def finish(settings, db, folder, platform):
    """Explicitly publish one already uploaded and processed Instagram container.

    The caller must collect a new confirmation. Saved captions and destination
    are reused; this function cannot create a container or upload video bytes.
    """
    if platform != "instagram":
        raise SocialError("Only Instagram containers need a separate finish step.")
    if not settings.instagram.enabled:
        raise SocialError("Enable Instagram publishing before finishing this Reel.")
    artifact, video = _artifact(folder)
    schema(db)
    record = _record(db, artifact["clip_id"], platform)
    if not record or record["state"] != "ready_to_publish" or record.get("publish_attempted"):
        raise SocialError("No processed Instagram container is available to finish. Refresh its saved status.")
    if (record["artifact_hash"] != stable_id(artifact) or record["video_sha256"] != sha256_file(video)
            or record["target_account_id"] != settings.instagram.target_account_id
            or record["api_version"] != settings.instagram.api_version):
        raise SocialError("The saved Instagram publication inputs changed. Reconcile the existing container.")
    credentials = _credentials(settings, platform)
    info = creator_info(settings, platform)
    if info["account_id"] != record["target_account_id"]:
        raise SocialError("The saved container belongs to a different Instagram account.")
    return _public(_instagram_finish(settings, db, record, credentials))
