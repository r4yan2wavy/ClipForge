"""YouTube metadata discovery only; no media acquisition or hosted inference.

Additional analytics are deliberately gated. The pure signal functions also accept
synthetic observations for offline tests; calling them is not platform permission.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import re
from statistics import median
import time
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import requests


UTC = timezone.utc
API_ROOT = "https://www.googleapis.com/youtube/v3/"
METRICS = {"views_to_subscribers", "recent_velocity", "velocity_lift", "comparable_age"}


class SourcingError(RuntimeError):
    """Sanitized API failure without credential-bearing request URLs."""


class BudgetExceeded(SourcingError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Observation timestamps must include a timezone")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _dt(value).isoformat()


def duration_seconds(value: str) -> float:
    """The day/hour/minute/second ISO-8601 durations used by videos.list."""
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match or not any(match.groups()):
        raise ValueError("Unsupported video duration")
    days, hours, minutes, seconds = (float(v or 0) for v in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class Observation:
    video_id: str
    channel_id: str
    published_at: datetime
    observed_at: datetime
    views: int
    subscribers: int | None = None
    hidden_subscribers: bool = False
    rounded_subscribers: bool = True

    def __post_init__(self) -> None:
        _dt(self.published_at)
        _dt(self.observed_at)
        if isinstance(self.views, bool) or not isinstance(self.views, int) or self.views < 0:
            raise ValueError("Views must be a nonnegative integer")
        if self.subscribers is not None and (
            isinstance(self.subscribers, bool) or not isinstance(self.subscribers, int) or self.subscribers < 0
        ):
            raise ValueError("Subscribers must be a nonnegative integer or missing")


def calculate_signals(
    observations: Sequence[Observation], baseline: Sequence[Observation] = (), *,
    velocity_floor: float = 1.0, min_subscribers: int = 1000,
    min_observation_minutes: float = 15, baseline_min_samples: int = 5,
    baseline_age_tolerance_hours: float = 2,
) -> dict[str, Any]:
    """Estimates for an eligible, approved use case (or synthetic offline inputs).

    Baselines use one nearest comparable-age snapshot per OTHER same-channel
    video. No retention, satisfaction, revenue or virality estimate is produced.
    """
    if not math.isfinite(velocity_floor) or velocity_floor <= 0:
        raise ValueError("velocity_floor must be finite and positive")
    if min_observation_minutes <= 0 or baseline_min_samples < 1 or baseline_age_tolerance_hours < 0:
        raise ValueError("Invalid signal sampling configuration")
    result: dict[str, Any] = {
        "label": "ClipForge estimates; not YouTube-provided metrics",
        "views_to_subscribers": None, "velocity_now": None,
        "velocity_before": None, "velocity_lift": None,
        "comparable_age_ratio": None, "baseline_samples": 0, "warnings": [],
    }
    if not observations:
        result["warnings"].append("insufficient_snapshots")
        return result
    ordered = sorted(observations, key=lambda row: _dt(row.observed_at))
    last = ordered[-1]
    if any(row.video_id != last.video_id or row.channel_id != last.channel_id for row in ordered):
        raise ValueError("A signal history must describe one video and channel")
    if any(_dt(row.observed_at) < _dt(row.published_at) for row in ordered):
        raise ValueError("Observation predates publication")
    if last.hidden_subscribers or last.subscribers is None or last.subscribers == 0:
        result["warnings"].append("subscriber_ratio_unavailable")
    elif last.subscribers < min_subscribers:
        result["warnings"].append("small_channel_ratio_suppressed")
    else:
        result["views_to_subscribers"] = last.views / last.subscribers
        if last.rounded_subscribers:
            result["warnings"].append("subscriber_count_rounded_ratio_approximate")

    # Keep actual times. Never fabricate another observation from a cache hit.
    if len(ordered) < 3:
        result["warnings"].append("insufficient_snapshots_for_velocity_lift")
    if len(ordered) >= 2:
        recent = ordered[-3:]
        if any(b.views < a.views for a, b in zip(recent, recent[1:])):
            result["warnings"].append("corrected_counter_restart_history")
        else:
            deltas = [(_dt(b.observed_at) - _dt(a.observed_at)).total_seconds() / 3600
                      for a, b in zip(recent, recent[1:])]
            if any(hours < min_observation_minutes / 60 for hours in deltas):
                result["warnings"].append("observations_too_close_or_duplicated")
            else:
                result["velocity_now"] = (recent[-1].views - recent[-2].views) / deltas[-1]
                if result["velocity_now"] == 0:
                    result["warnings"].append("unchanged_or_delayed_statistics")
                if len(recent) == 3:
                    before = (recent[1].views - recent[0].views) / deltas[0]
                    result["velocity_before"] = before
                    result["velocity_lift"] = result["velocity_now"] / max(before, velocity_floor)

    age_hours = (_dt(last.observed_at) - _dt(last.published_at)).total_seconds() / 3600
    nearest: dict[str, tuple[float, int]] = {}
    for sample in baseline:
        if sample.channel_id != last.channel_id or sample.video_id == last.video_id:
            continue
        sample_age = (_dt(sample.observed_at) - _dt(sample.published_at)).total_seconds() / 3600
        distance = abs(sample_age - age_hours)
        if sample_age < 0 or distance > baseline_age_tolerance_hours:
            continue
        if sample.video_id not in nearest or distance < nearest[sample.video_id][0]:
            nearest[sample.video_id] = (distance, sample.views)
    result["baseline_samples"] = len(nearest)
    if len(nearest) < baseline_min_samples:
        result["warnings"].append("insufficient_comparable_age_baseline")
    else:
        typical = median(views for _, views in nearest.values())
        if typical > 0:
            result["comparable_age_ratio"] = last.views / typical
        else:
            result["warnings"].append("zero_comparable_age_baseline")
    return result


def approval_status(settings: Any) -> tuple[bool, str]:
    """Validate local evidence completeness/integrity, never confer permission.

    The operator must independently verify issuer, project and scope. A forged or
    inapplicable record does not become valid merely by passing these checks.
    """
    cfg = settings.discovery
    if not cfg.derived_metrics_enabled:
        return False, "additional_analytics_disabled"
    if not cfg.approval_reference:
        return False, "approval_evidence_missing"
    try:
        path = settings.resolve(cfg.approval_reference)
        record = json.loads(path.read_text(encoding="utf-8"))
        required = ("project_number", "reviewed_by", "approval_reference", "evidence_path", "evidence_sha256")
        if not all(isinstance(record.get(key), str) and record[key].strip() for key in required):
            return False, "approval_record_incomplete"
        if record.get("issuer") != "YouTube" or record.get("authorized_use_case") != "clipforge_discovery_trends":
            return False, "approval_scope_mismatch"
        if not METRICS.issubset(set(record.get("approved_metrics", []))):
            return False, "approval_metrics_incomplete"
        if _dt(record["amendment_accepted_at"]) > _now() or _dt(record["reviewed_at"]) > _now():
            return False, "approval_record_dates_invalid"
        if record.get("expires_at") and _dt(record["expires_at"]) <= _now():
            return False, "approval_record_expired"
        evidence_path = (path.parent / record["evidence_path"]).resolve()
        evidence = evidence_path.read_bytes()
        if not evidence or hashlib.sha256(evidence).hexdigest() != record["evidence_sha256"]:
            return False, "approval_evidence_integrity_failed"
        return True, "operator_verified_evidence_record_present_not_independent_approval_verification"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "approval_evidence_invalid"


def _schema(db: Any) -> None:
    db.conn.execute("PRAGMA secure_delete=ON")
    db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_cache (
            cache_key TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_runs (
            run_key TEXT PRIMARY KEY, completed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_budget (
            day TEXT NOT NULL, bucket TEXT NOT NULL, used INTEGER NOT NULL,
            PRIMARY KEY(day,bucket));
        CREATE TABLE IF NOT EXISTS discovery_channels (
            channel_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_videos (
            video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, published_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS discovery_snapshots (
            video_id TEXT NOT NULL, channel_id TEXT NOT NULL, published_at TEXT NOT NULL,
            observed_at TEXT NOT NULL, views INTEGER NOT NULL, subscribers INTEGER,
            hidden_subscribers INTEGER NOT NULL, PRIMARY KEY(video_id,observed_at));
        CREATE INDEX IF NOT EXISTS discovery_snapshots_age ON discovery_snapshots(channel_id,observed_at);
        CREATE TABLE IF NOT EXISTS discovery_signals (
            video_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, streak INTEGER NOT NULL,
            config_hash TEXT NOT NULL, payload TEXT NOT NULL);
    """)
    db.conn.commit()


def purge(db: Any, retention_days: int = 28, now: datetime | None = None) -> dict[str, int]:
    """Delete API metadata, historical observations and estimates within 30 days.

    Run daily even while discovery is disabled; historical snapshots are deleted,
    not 'refreshed' into newly dated copies. Local media/manifests are separate.
    """
    if not 1 <= retention_days <= 30:
        raise ValueError("Public discovery retention_days must be 1..30")
    _schema(db)
    cutoff = _iso((now or _now()) - timedelta(days=retention_days))
    deleted = {}
    with db.transaction():
        for table, column in (("discovery_cache", "fetched_at"), ("discovery_channels", "fetched_at"),
                              ("discovery_videos", "fetched_at"), ("discovery_snapshots", "observed_at"),
                              ("discovery_signals", "observed_at"), ("discovery_runs", "completed_at")):
            deleted[table] = db.conn.execute(f"DELETE FROM {table} WHERE {column} <= ?", (cutoff,)).rowcount
        db.conn.execute("DELETE FROM discovery_budget WHERE day < ?", (cutoff[:10],))
    # The single-machine caller holds an OS lock, so no competing writer should
    # prevent this checkpoint. Protect/expire external backups separately.
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted


def forget_discovery(db: Any, video_id: str | None = None) -> dict[str, int]:
    """Immediately erase public discovery records, optionally for one video.

    Clears the whole response cache conservatively, because playlist/search
    pages may embed the target ID or title. Channel metadata for the target's
    channel is also removed. Aggregate daily request counters remain, so deletion
    cannot reset the budget. This is erasure, not a permanent discovery opt-out:
    change configured channels/search terms to prevent subsequent acquisition.
    Authorized local originals/manifests and publishing history are separate.
    Caller must hold the project OS lock and manage independent exports/backups.
    """
    if video_id is not None and (not isinstance(video_id, str) or not video_id.strip()):
        raise ValueError("Provide a nonempty video ID, or None to forget all discovery")
    _schema(db)
    deleted: dict[str, int] = {}
    with db.transaction():
        # Cached query pages can reference videos not present in the current
        # candidate list; removing every response is safer than partial matching.
        deleted["discovery_cache"] = db.conn.execute("DELETE FROM discovery_cache").rowcount
        deleted["discovery_runs"] = db.conn.execute("DELETE FROM discovery_runs").rowcount
        if video_id is None:
            for table in ("discovery_videos", "discovery_snapshots", "discovery_signals", "discovery_channels"):
                deleted[table] = db.conn.execute(f"DELETE FROM {table}").rowcount
        else:
            channels = {row[0] for row in db.conn.execute(
                "SELECT channel_id FROM discovery_videos WHERE video_id=? UNION SELECT channel_id FROM discovery_snapshots WHERE video_id=?",
                (video_id, video_id))}
            for table in ("discovery_videos", "discovery_snapshots", "discovery_signals"):
                deleted[table] = db.conn.execute(f"DELETE FROM {table} WHERE video_id=?", (video_id,)).rowcount
            deleted["discovery_channels"] = sum(
                db.conn.execute("DELETE FROM discovery_channels WHERE channel_id=?", (channel_id,)).rowcount
                for channel_id in channels)
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return deleted


class YouTube:
    def __init__(self, settings: Any, db: Any, *, force: bool = False):
        self.cfg, self.db, self.force = settings.discovery, db, force
        self.key = os.environ.get(self.cfg.api_key_env, "")
        self.calls = 0
        self.cache_hits = 0
        if not self.key:
            raise SourcingError(f"Set the {self.cfg.api_key_env} environment variable for API discovery")

    def _spend(self, resource: str) -> None:
        bucket = "search" if resource == "search" else "other"
        limit = getattr(self.cfg, "search_daily_budget", 100) if bucket == "search" else self.cfg.daily_budget_units
        cost = getattr(self.cfg, "search_call_cost", 1) if bucket == "search" else getattr(self.cfg, "general_read_call_cost", 1)
        if cost < 1:
            raise ValueError("Request budget costs must be positive")
        day = _now().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
        with self.db.transaction():
            row = self.db.conn.execute("SELECT used FROM discovery_budget WHERE day=? AND bucket=?", (day, bucket)).fetchone()
            if (row["used"] if row else 0) + cost > limit:
                raise BudgetExceeded(f"Local {bucket} daily budget exhausted; resume after Pacific midnight")
            if limit <= 0:
                raise BudgetExceeded(f"Local {bucket} daily budget disabled")
            self.db.conn.execute("INSERT INTO discovery_budget VALUES(?,?,?) ON CONFLICT(day,bucket) DO UPDATE SET used=used+excluded.used", (day, bucket, cost))

    def get(self, resource: str, **params: Any) -> tuple[dict[str, Any], str, bool]:
        cache_key = hashlib.sha256(json.dumps([resource, params], sort_keys=True).encode()).hexdigest()
        row = self.db.conn.execute("SELECT * FROM discovery_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row and not self.force and _dt(row["fetched_at"]) > _now() - timedelta(minutes=self.cfg.cache_minutes):
            self.cache_hits += 1
            return json.loads(row["payload"]), row["fetched_at"], False
        for attempt in range(3):
            self._spend(resource)  # Failed/retried requests also consume budget.
            self.calls += 1
            try:
                response = requests.get(API_ROOT + resource, params={**params, "key": self.key}, timeout=(10, 45))
            except requests.RequestException:
                if attempt == 2:
                    raise SourcingError("API network failure after finite retries") from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == 2:
                    raise SourcingError(f"Transient API HTTP {response.status_code}; retry next run")
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                # Never expose response URLs, exception strings or Google messages.
                category = "permanent_request"
                if response.status_code == 401:
                    category = "authentication"
                elif response.status_code == 403:
                    category = "permission_or_project_configuration"
                    try:
                        reasons = {error.get("reason") for error in response.json().get("error", {}).get("errors", [])}
                        if reasons & {"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"}:
                            category = "quota_exhausted"
                        elif reasons & {"keyInvalid", "authError", "accessNotConfigured", "ipRefererBlocked"}:
                            category = "authentication_or_project_configuration"
                    except (ValueError, TypeError, AttributeError):
                        pass
                raise SourcingError(f"YouTube {category} failure, HTTP {response.status_code}; inspect Cloud console")
            try:
                payload = response.json()
            except ValueError:
                raise SourcingError("API returned invalid JSON") from None
            if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                raise SourcingError("API returned an unexpected response")
            fetched_at = _iso(_now())
            with self.db.transaction():
                self.db.conn.execute("INSERT OR REPLACE INTO discovery_cache VALUES(?,?,?)", (cache_key, fetched_at, json.dumps(payload)))
            return payload, fetched_at, True
        raise SourcingError("API retries exhausted")

    def pages(self, resource: str, **params: Any):
        token = None
        seen = set()
        for _ in range(self.cfg.max_pages):
            payload, observed_at, fresh = self.get(resource, **params, **({"pageToken": token} if token else {}))
            yield payload, observed_at, fresh
            token = payload.get("nextPageToken")
            if not token or token in seen:
                break
            seen.add(token)


def _due(db: Any, key: str, hours: float, force: bool) -> bool:
    row = db.conn.execute("SELECT completed_at FROM discovery_runs WHERE run_key=?", (key,)).fetchone()
    return force or not row or _dt(row["completed_at"]) <= _now() - timedelta(hours=hours)


def _done(db: Any, key: str) -> None:
    with db.transaction():
        db.conn.execute("INSERT OR REPLACE INTO discovery_runs VALUES(?,?)", (key, _iso(_now())))


def _batches(values: Sequence[str], size: int = 50):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _filter(item: dict[str, Any], cfg: Any) -> str | None:
    snippet = item.get("snippet", {})
    if snippet.get("liveBroadcastContent", "none") != "none" or item.get("liveStreamingDetails"):
        return "live_stream"
    try:
        age = (_now() - _dt(snippet["publishedAt"])).total_seconds() / 86400
        duration = duration_seconds(item["contentDetails"]["duration"])
    except (KeyError, ValueError, TypeError):
        return "missing_metadata"
    if age < 0 or age > cfg.max_age_days:
        return "publication_age"
    if not cfg.min_duration_s <= duration <= cfg.max_duration_s:
        return "duration"
    language = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage")
    if cfg.language and not language:
        return "unknown_language_review"  # relevanceLanguage is only a search hint.
    if cfg.language and language.split("-")[0].lower() != cfg.language.split("-")[0].lower():
        return "language"
    return None


def _observation(row: Any) -> Observation:
    return Observation(row["video_id"], row["channel_id"], _dt(row["published_at"]), _dt(row["observed_at"]),
                       row["views"], row["subscribers"], bool(row["hidden_subscribers"]))


def _update_signal(db: Any, cfg: Any, video_id: str, observed_at: str) -> dict[str, Any]:
    rows = db.conn.execute("SELECT * FROM discovery_snapshots WHERE video_id=? ORDER BY observed_at DESC LIMIT 3", (video_id,)).fetchall()
    history = [_observation(row) for row in rows]
    baseline = [_observation(row) for row in db.conn.execute(
        "SELECT * FROM discovery_snapshots WHERE channel_id=? AND video_id!=?", (history[0].channel_id, video_id))]
    signals = calculate_signals(history, baseline, velocity_floor=cfg.velocity_floor, min_subscribers=cfg.min_subscribers,
        min_observation_minutes=getattr(cfg, "min_observation_minutes", 15),
        baseline_min_samples=getattr(cfg, "baseline_min_samples", 5),
        baseline_age_tolerance_hours=getattr(cfg, "baseline_age_tolerance_hours", 2))
    threshold_ratio = getattr(cfg, "baseline_ratio_threshold", 1.5)
    qualifies = (signals["velocity_now"] is not None and signals["velocity_now"] >= cfg.threshold_velocity
                 and signals["velocity_lift"] is not None and signals["velocity_lift"] >= cfg.threshold_lift
                 and signals["comparable_age_ratio"] is not None and signals["comparable_age_ratio"] >= threshold_ratio)
    signature = hashlib.sha256(json.dumps({"config": cfg.model_dump() if hasattr(cfg, "model_dump") else vars(cfg)}, sort_keys=True, default=str).encode()).hexdigest()
    previous = db.conn.execute("SELECT * FROM discovery_signals WHERE video_id=?", (video_id,)).fetchone()
    if previous and previous["observed_at"] == observed_at:
        return json.loads(previous["payload"])
    max_gap = getattr(cfg, "max_confirmation_gap_hours", max(3, cfg.monitor_every_minutes / 20))
    continuous = bool(previous and previous["config_hash"] == signature and
                      (_dt(observed_at) - _dt(previous["observed_at"])).total_seconds() <= max_gap * 3600)
    streak = ((previous["streak"] if continuous else 0) + 1) if qualifies else 0
    signals.update(streak=streak, promoted=streak >= cfg.confirmation_count)
    with db.transaction():
        db.conn.execute("INSERT OR REPLACE INTO discovery_signals VALUES(?,?,?,?,?)",
                        (video_id, observed_at, streak, signature, json.dumps(signals)))
    return signals


def discover(settings: Any, db: Any, force: bool = False) -> dict[str, Any]:
    """Fetch public metadata. Discovery never authorizes media use or downloads.

    Caller holds the project's OS lock; this function uses transactional budgets.
    Returned candidates are in waiting_media until a separate authorized manifest
    supplies a local original. Local budgets do not replace Cloud project quotas.
    """
    cfg = settings.discovery
    summary: dict[str, Any] = {"candidates": [], "filtered": {}, "unavailable": [], "errors": [], "api_calls": 0, "cache_hits": 0, "fresh_snapshots": 0}
    summary["purged"] = purge(db, cfg.retention_days)
    eligible, reason = approval_status(settings)
    summary["additional_analytics"] = {"eligible_record": eligible, "reason": reason}
    if not eligible:
        with db.transaction():
            db.conn.execute("DELETE FROM discovery_signals")
    if not cfg.channel_ids and not cfg.search_terms:
        return summary
    try:
        api = YouTube(settings, db, force=force)
    except SourcingError as exc:
        summary["errors"].append(str(exc))
        return summary
    ids: set[str] = set()
    fresh_ids: set[str] = set()
    monitor_due = _due(db, "monitor", cfg.monitor_every_minutes / 60, force)
    try:
        if monitor_due:
            for group in _batches(list(dict.fromkeys(cfg.channel_ids))):
                payload, fetched_at, _ = api.get("channels", part="contentDetails,statistics", id=",".join(group), maxResults=50)
                found = {item["id"]: item for item in payload.get("items", [])}
                for channel_id in group:
                    item = found.get(channel_id)
                    if not item:
                        summary["unavailable"].append({"channel_id": channel_id})
                        with db.transaction():
                            db.conn.execute("DELETE FROM discovery_channels WHERE channel_id=?", (channel_id,))
                        continue
                    with db.transaction():
                        db.conn.execute("INSERT OR REPLACE INTO discovery_channels VALUES(?,?,?)", (channel_id, fetched_at, json.dumps(item)))
                    playlist_id = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                    if not playlist_id:
                        continue
                    for page, _, _ in api.pages("playlistItems", part="contentDetails", playlistId=playlist_id, maxResults=cfg.page_size):
                        ids.update(row["contentDetails"]["videoId"] for row in page.get("items", []) if row.get("contentDetails", {}).get("videoId"))
            # Follow previously discovered videos even if they fall off page one.
            ids.update(row["video_id"] for row in db.conn.execute("SELECT video_id FROM discovery_videos"))
            _done(db, "monitor")
        for term in cfg.search_terms:
            run_key = "search:" + hashlib.sha256(term.encode()).hexdigest()
            if not _due(db, run_key, cfg.search_every_hours, force):
                continue
            params: dict[str, Any] = dict(part="snippet", type="video", order="date", q=term,
                publishedAfter=_iso(_now() - timedelta(days=cfg.max_age_days)), maxResults=cfg.page_size)
            if cfg.language:
                params["relevanceLanguage"] = cfg.language
            for page, _, _ in api.pages("search", **params):
                ids.update(row["id"]["videoId"] for row in page.get("items", []) if row.get("id", {}).get("videoId"))
            _done(db, run_key)
        for group in _batches(sorted(ids)):
            payload, observed_at, fresh = api.get("videos", part="snippet,contentDetails,statistics,liveStreamingDetails", id=",".join(group))
            found = {item["id"]: item for item in payload.get("items", [])}
            for video_id in group:
                item = found.get(video_id)
                if not item:
                    summary["unavailable"].append({"video_id": video_id})
                    with db.transaction():
                        for table in ("discovery_videos", "discovery_snapshots", "discovery_signals"):
                            db.conn.execute(f"DELETE FROM {table} WHERE video_id=?", (video_id,))
                    continue
                rejected = _filter(item, cfg)
                if rejected:
                    summary["filtered"][rejected] = summary["filtered"].get(rejected, 0) + 1
                    with db.transaction():
                        db.conn.execute("DELETE FROM discovery_videos WHERE video_id=?", (video_id,))
                    continue
                snippet = item["snippet"]
                with db.transaction():
                    db.conn.execute("INSERT OR REPLACE INTO discovery_videos VALUES(?,?,?,?,?,?)", (
                        video_id, snippet["channelId"], _iso(_dt(snippet["publishedAt"])), observed_at, json.dumps(item), "waiting_media"))
                summary["candidates"].append({"video_id": video_id, "channel_id": snippet["channelId"], "title": snippet.get("title", ""), "state": "waiting_media"})
                if fresh:
                    fresh_ids.add(video_id)

        # Subscribers for search-discovered channels; one batch per 50 channels.
        channel_ids = sorted({row["channel_id"] for row in db.conn.execute("SELECT channel_id FROM discovery_videos")})
        needed = []
        for channel_id in channel_ids:
            row = db.conn.execute("SELECT fetched_at FROM discovery_channels WHERE channel_id=?", (channel_id,)).fetchone()
            if not row or force or _dt(row["fetched_at"]) <= _now() - timedelta(minutes=cfg.cache_minutes):
                needed.append(channel_id)
        if fresh_ids:
            for group in _batches(needed):
                payload, fetched_at, _ = api.get("channels", part="contentDetails,statistics", id=",".join(group), maxResults=50)
                with db.transaction():
                    for item in payload.get("items", []):
                        db.conn.execute("INSERT OR REPLACE INTO discovery_channels VALUES(?,?,?)", (item["id"], fetched_at, json.dumps(item)))
        # Only fresh videos.list responses create observations. Cache payloads
        # preserve their old timestamp and cannot advance trend confirmation.
        for video_id in sorted(fresh_ids):
            row = db.conn.execute("SELECT * FROM discovery_videos WHERE video_id=?", (video_id,)).fetchone()
            item = json.loads(row["payload"])
            views = item.get("statistics", {}).get("viewCount")
            if views is None:
                continue
            channel = db.conn.execute("SELECT payload FROM discovery_channels WHERE channel_id=?", (row["channel_id"],)).fetchone()
            stats = json.loads(channel["payload"]).get("statistics", {}) if channel else {}
            hidden = bool(stats.get("hiddenSubscriberCount", False))
            subscribers = int(stats["subscriberCount"]) if not hidden and stats.get("subscriberCount") is not None else None
            with db.transaction():
                db.conn.execute("INSERT OR IGNORE INTO discovery_snapshots VALUES(?,?,?,?,?,?,?)", (
                    video_id, row["channel_id"], row["published_at"], row["fetched_at"], int(views), subscribers, int(hidden)))
            summary["fresh_snapshots"] += 1
            if eligible:
                _update_signal(db, cfg, video_id, row["fetched_at"])
    except (SourcingError, ValueError, KeyError, TypeError) as exc:
        summary["errors"].append(str(exc) if isinstance(exc, SourcingError) else "Malformed API data; review discovery records")
    summary["api_calls"], summary["cache_hits"] = api.calls, api.cache_hits
    return summary
