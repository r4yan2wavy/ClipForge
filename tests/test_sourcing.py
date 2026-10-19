from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from clipforge.common import DB, Settings
from clipforge import sourcing as src


NOW = datetime(2026, 10, 18, 20, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = Settings()
    settings.paths.root = str(tmp_path)
    settings.discovery.channel_ids = ["channel"]
    monkeypatch.setenv("YOUTUBE_API_KEY", "never-print-this-key")
    clock = [NOW]
    monkeypatch.setattr(src, "_now", lambda: clock[0])
    db = DB(tmp_path / "test.sqlite3")
    yield settings, db, clock
    db.close()


def obs(views, hours, *, video="video", subscribers=1000, hidden=False):
    return src.Observation(video, "channel", NOW - timedelta(days=1), NOW + timedelta(hours=hours),
                           views, subscribers, hidden)


def video(video_id="video", **changes):
    value = {"id": video_id, "snippet": {"channelId": "channel", "title": "Fixture",
             "publishedAt": (NOW - timedelta(days=1)).isoformat(), "defaultAudioLanguage": "en",
             "liveBroadcastContent": "none"}, "contentDetails": {"duration": "PT10M"},
             "statistics": {"viewCount": "1000"}}
    value.update(changes)
    return value


def fake_response(payload, status=200):
    return SimpleNamespace(status_code=status, json=lambda: payload)


def test_actual_elapsed_times_and_distinct_age_matched_baseline():
    history = [obs(100, 0), obs(200, 2), obs(600, 3)]
    baseline = [obs(300, 3, video=f"other{i}") for i in range(5)]
    baseline += [obs(999999, 10, video="wrong-age"), obs(600, 3)]
    result = src.calculate_signals(history, baseline)
    assert result["velocity_before"] == 50
    assert result["velocity_now"] == 400
    assert result["velocity_lift"] == 8
    assert result["comparable_age_ratio"] == 2
    assert result["baseline_samples"] == 5
    assert result["views_to_subscribers"] == .6
    assert "subscriber_count_rounded_ratio_approximate" in result["warnings"]


@pytest.mark.parametrize("subscribers,hidden", [(None, False), (0, False), (1000, True), (10, False)])
def test_missing_hidden_zero_small_subscribers(subscribers, hidden):
    result = src.calculate_signals([obs(100, 0, subscribers=subscribers, hidden=hidden)])
    assert result["views_to_subscribers"] is None
    assert result["velocity_lift"] is None


def test_corrected_delayed_and_duplicate_observations():
    corrected = src.calculate_signals([obs(1000, 0), obs(900, 1), obs(1100, 2)])
    assert corrected["velocity_now"] is None
    assert "corrected_counter_restart_history" in corrected["warnings"]
    delayed = src.calculate_signals([obs(100, 0), obs(100, 1), obs(100, 2)])
    assert delayed["velocity_now"] == 0
    assert delayed["velocity_lift"] == 0
    assert "unchanged_or_delayed_statistics" in delayed["warnings"]
    duplicate = src.calculate_signals([obs(100, 0), obs(200, 0), obs(300, 1)])
    assert duplicate["velocity_now"] is None


def test_never_reuse_current_totals_as_historical_baseline():
    # Five snapshots of one video are one baseline sample, not five peers.
    baseline = [obs(50, hours / 10, video="one-peer") for hours in range(5)]
    result = src.calculate_signals([obs(100, 0)], baseline)
    assert result["baseline_samples"] == 1
    assert result["comparable_age_ratio"] is None


def test_uploads_playlist_cache_and_repeat_run(setup, monkeypatch):
    settings, db, clock = setup
    calls = []

    def get(url, params, timeout):
        resource = url.rsplit("/", 1)[-1]
        calls.append(resource)
        if resource == "channels":
            return fake_response({"items": [{"id": "channel", "contentDetails": {"relatedPlaylists": {"uploads": "uploads"}},
                "statistics": {"subscriberCount": "1000", "hiddenSubscriberCount": False}}]})
        if resource == "playlistItems":
            return fake_response({"items": [{"contentDetails": {"videoId": "video"}}]})
        if resource == "videos":
            return fake_response({"items": [video()]})
        pytest.fail("Known-channel monitor must use uploads playlists")

    monkeypatch.setattr(src.requests, "get", get)
    first = src.discover(settings, db)
    assert first["api_calls"] == 3
    assert first["candidates"][0]["state"] == "waiting_media"
    assert db.conn.execute("SELECT COUNT(*) FROM discovery_snapshots").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM discovery_signals").fetchone()[0] == 0
    assert src.discover(settings, db)["api_calls"] == 0
    # Explicit immediate cached videos request preserves timestamp and reports no fresh fetch.
    api = src.YouTube(settings, db)
    _, observed_at, fresh = api.get("videos", part="snippet,contentDetails,statistics,liveStreamingDetails", id="video")
    assert not fresh and src._dt(observed_at) == NOW
    clock[0] += timedelta(hours=1)
    next_run = src.discover(settings, db)
    assert next_run["api_calls"] == 3
    assert db.conn.execute("SELECT COUNT(*) FROM discovery_snapshots").fetchone()[0] == 2
    assert "search" not in calls


def test_search_pagination_and_unavailable_batched_videos(setup, monkeypatch):
    settings, db, _ = setup
    settings.discovery.channel_ids = []
    settings.discovery.search_terms = ["fixture topic"]
    pages, batches = [], []

    def get(url, params, timeout):
        resource = url.rsplit("/", 1)[-1]
        if resource == "search":
            pages.append(params.get("pageToken"))
            ids = range(0, 50) if "pageToken" not in params else range(50, 53)
            return fake_response({"items": [{"id": {"videoId": str(i)}} for i in ids],
                                  **({"nextPageToken": "page2"} if "pageToken" not in params else {})})
        if resource == "videos":
            ids = params["id"].split(",")
            batches.append(ids)
            return fake_response({"items": [video(i) for i in ids if i != "52"]})
        return fake_response({"items": []})

    monkeypatch.setattr(src.requests, "get", get)
    result = src.discover(settings, db)
    assert pages == [None, "page2"]
    assert [len(ids) for ids in batches] == [50, 3]
    assert {"video_id": "52"} in result["unavailable"]
    assert len(result["candidates"]) == 52
    assert db.conn.execute("SELECT used FROM discovery_budget WHERE bucket='search'").fetchone()[0] == 2


def test_separate_quota_budgets_and_no_credential_leak(setup, monkeypatch):
    settings, db, _ = setup
    settings.discovery.search_daily_budget = 1
    src._schema(db)
    monkeypatch.setattr(src.requests, "get", lambda *args, **kwargs: fake_response({"items": []}, 403))
    api = src.YouTube(settings, db)
    with pytest.raises(src.SourcingError) as error:
        api.get("search", part="snippet", q="test")
    assert "never-print" not in str(error.value)
    with pytest.raises(src.BudgetExceeded):
        api.get("search", part="snippet", q="another")
    # Search depletion leaves the general read bucket available.
    api._spend("videos")
    assert db.conn.execute("SELECT used FROM discovery_budget WHERE bucket='other'").fetchone()[0] == 1


def test_purge_deletes_snapshots_instead_of_redating(setup):
    settings, db, _ = setup
    src._schema(db)
    old = src._iso(NOW - timedelta(days=29))
    with db.transaction():
        db.conn.execute("INSERT INTO discovery_snapshots VALUES(?,?,?,?,?,?,?)", ("old", "channel", old, old, 100, None, 1))
        db.conn.execute("INSERT INTO discovery_cache VALUES(?,?,?)", ("old", old, '{"items": []}'))
    result = src.purge(db, 28, NOW)
    assert result["discovery_snapshots"] == result["discovery_cache"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM discovery_snapshots").fetchone()[0] == 0
    with pytest.raises(ValueError):
        src.purge(db, 31)


def test_flag_does_not_grant_permission_and_evidence_integrity(setup, tmp_path):
    settings, _, _ = setup
    settings.discovery.derived_metrics_enabled = True
    assert src.approval_status(settings) == (False, "approval_evidence_missing")
    evidence = tmp_path / "synthetic-test-evidence.txt"
    evidence.write_text("Synthetic fixture, not actual YouTube approval", encoding="utf-8")
    record = {"issuer": "YouTube", "authorized_use_case": "clipforge_discovery_trends", "project_number": "synthetic",
        "reviewed_by": "Synthetic reviewer", "approval_reference": "synthetic-not-real",
        "amendment_accepted_at": src._iso(NOW - timedelta(days=1)), "reviewed_at": src._iso(NOW - timedelta(days=1)),
        "approved_metrics": sorted(src.METRICS), "evidence_path": evidence.name,
        "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    settings.discovery.approval_reference = path.name
    assert src.approval_status(settings)[0]  # Completeness/integrity only, not issuer verification.
    evidence.write_text("modified", encoding="utf-8")
    assert src.approval_status(settings) == (False, "approval_evidence_integrity_failed")


def test_filters_age_duration_unknown_language_and_livestream(setup):
    settings, _, _ = setup
    assert src._filter(video(), settings.discovery) is None
    assert src._filter(video(contentDetails={"duration": "PT1M"}), settings.discovery) == "duration"
    assert src._filter(video(liveStreamingDetails={"actualStartTime": "anything"}), settings.discovery) == "live_stream"
    item = video()
    del item["snippet"]["defaultAudioLanguage"]
    assert src._filter(item, settings.discovery) == "unknown_language_review"


def test_sustained_confirmation_counts_only_new_observations(setup, monkeypatch):
    settings, db, _ = setup
    src._schema(db)
    settings.discovery.confirmation_count = 3
    monkeypatch.setattr(src, "calculate_signals", lambda *args, **kwargs: {
        "velocity_now": 1000, "velocity_lift": 3, "comparable_age_ratio": 2})
    with db.transaction():
        db.conn.execute("INSERT INTO discovery_snapshots VALUES(?,?,?,?,?,?,?)", ("video", "channel", src._iso(NOW-timedelta(days=1)), src._iso(NOW), 100, 1000, 0))
    first = src._update_signal(db, settings.discovery, "video", src._iso(NOW))
    again = src._update_signal(db, settings.discovery, "video", src._iso(NOW))
    assert first["streak"] == again["streak"] == 1
    second = src._update_signal(db, settings.discovery, "video", src._iso(NOW+timedelta(hours=1)))
    third = src._update_signal(db, settings.discovery, "video", src._iso(NOW+timedelta(hours=2)))
    assert not second["promoted"] and third["promoted"]
    reset = src._update_signal(db, settings.discovery, "video", src._iso(NOW+timedelta(hours=10)))
    assert reset["streak"] == 1 and not reset["promoted"]


def test_immediate_targeted_and_all_discovery_erasure_preserves_budget(setup):
    _, db, _ = setup
    src._schema(db)
    stamp = src._iso(NOW)
    with db.transaction():
        for video_id, channel_id in (("forget", "target-channel"), ("keep", "other-channel")):
            db.conn.execute("INSERT INTO discovery_videos VALUES(?,?,?,?,?,?)", (video_id, channel_id, stamp, stamp, "{}", "waiting_media"))
            db.conn.execute("INSERT INTO discovery_snapshots VALUES(?,?,?,?,?,?,?)", (video_id, channel_id, stamp, stamp, 100, None, 1))
            db.conn.execute("INSERT INTO discovery_signals VALUES(?,?,?,?,?)", (video_id, stamp, 1, "hash", "{}"))
            db.conn.execute("INSERT INTO discovery_channels VALUES(?,?,?)", (channel_id, stamp, "{}"))
        db.conn.execute("INSERT INTO discovery_cache VALUES(?,?,?)", ("page", stamp, '{"title": "forget"}'))
        db.conn.execute("INSERT INTO discovery_runs VALUES(?,?)", ("search", stamp))
        db.conn.execute("INSERT INTO discovery_budget VALUES(?,?,?)", ("2026-10-18", "search", 4))
    targeted = src.forget_discovery(db, "forget")
    assert targeted["discovery_cache"] == targeted["discovery_channels"] == 1
    for table in ("discovery_videos", "discovery_snapshots", "discovery_signals"):
        assert db.conn.execute(f"SELECT video_id FROM {table}").fetchone()[0] == "keep"
    assert db.conn.execute("SELECT channel_id FROM discovery_channels").fetchone()[0] == "other-channel"
    assert db.conn.execute("SELECT used FROM discovery_budget").fetchone()[0] == 4
    erased = src.forget_discovery(db)
    assert erased["discovery_videos"] == erased["discovery_snapshots"] == erased["discovery_signals"] == erased["discovery_channels"] == 1
    assert db.conn.execute("SELECT used FROM discovery_budget").fetchone()[0] == 4
    with pytest.raises(ValueError):
        src.forget_discovery(db, "")
