"""Unknown work, changing counters and old attempts must not imply a deadline."""
import math

import pytest

from clipforge.progress import finish_timing, public_timing, start_stage, update_progress


def job(identifier="current", stage="downloading", work=None):
    value = {"id": identifier, "kind": "clip", "mode": "ai", "state": "running",
             "timing_profile": {"device": "cpu", "model": "local-small"},
             "stage_work_totals": {stage: work}}
    start_stage(value, stage, now=100)
    return value


def test_waiting_does_not_count_as_processing_and_retry_starts_a_new_attempt():
    value = {"state": "queued"}
    assert public_timing(value, now=100)["elapsed_s"] == 0
    value["state"] = "running"
    start_stage(value, "rendering", now=100)
    assert public_timing(value, now=125)["elapsed_s"] == 25
    finish_timing(value, now=130)
    value["state"] = "completed"
    assert public_timing(value, now=999)["elapsed_s"] == 30
    start_stage(value, "queued", now=1000)
    value["state"] = "queued"
    assert public_timing(value, now=1100)["elapsed_s"] == 0
    value["state"] = "running"
    start_stage(value, "rendering", now=1200)
    assert public_timing(value, now=1201)["elapsed_s"] == 1


def test_measured_eta_needs_multiple_real_samples_and_is_for_the_current_stage():
    value = job()
    update_progress(value, 0, 1000, now=100)
    update_progress(value, 100, 1000, now=101)
    assert public_timing(value, now=101)["eta_seconds"] is None
    update_progress(value, 300, 1000, now=103)
    display = public_timing(value, now=103)
    assert display["eta_seconds"] == 7
    assert display["stage_percent"] == 30
    assert display["estimate_source"] == "measured"
    assert display["eta_scope"] == "stage"
    start_stage(value, "merging", now=105)
    assert public_timing(value, now=106)["eta_seconds"] is None
    assert public_timing(value, now=106)["elapsed_s"] == 6
    assert public_timing(value, now=106)["stage_elapsed_s"] == 1


@pytest.mark.parametrize("completed,total", [(0, None), (0, 0), (10, None), (10, 0),
                                              (math.nan, 100), (math.inf, 100),
                                              (True, 100), (-1, 100), (110, 100)])
def test_unknown_and_invalid_work_never_produces_an_eta(completed, total):
    value = job()
    for instant in (100, 102, 104):
        update_progress(value, completed, total, now=instant)
    display = public_timing(value, now=104)
    assert display["eta_seconds"] is None
    assert display["stage_percent"] is None


def test_stream_counter_reset_and_wire_overhead_disable_the_entire_stage():
    value = job()
    for index, amount in enumerate((0, 100, 300)):
        update_progress(value, amount, 1000, now=100 + index * 2)
    assert public_timing(value, now=104)["eta_seconds"] is not None
    update_progress(value, 0, 1000, now=106)
    for index, amount in enumerate((100, 200, 300)):
        update_progress(value, amount, 1000, now=108 + index * 2)
    assert public_timing(value, now=112)["eta_seconds"] is None
    assert public_timing(value, now=112)["stage_percent"] is None
    start_stage(value, "verifying", now=114)
    update_progress(value, 1001, 1000, now=114)
    assert public_timing(value, now=114)["stage_percent"] is None


def test_repeated_stage_notifications_preserve_samples_but_not_stalled_estimates():
    value = job()
    for instant, amount in ((100, 0), (102, 100), (104, 200)):
        start_stage(value, "downloading", now=instant)
        update_progress(value, amount, 1000, now=instant)
    assert public_timing(value, now=104)["eta_seconds"] == 16
    for instant in (110, 120):
        update_progress(value, 200, 1000, now=instant)
    display = public_timing(value, now=120)
    assert display["eta_seconds"] is None
    assert display["timing_status"] == "stalled"
    value["state"] = "interrupted"
    assert public_timing(value, now=999)["elapsed_s"] == 20


def completed(identifier, duration, total=60, device="cpu"):
    value = job(identifier, "transcribing", total)
    value["timing_profile"]["device"] = device
    for fraction in (0, .25, .5, .75, 1):
        update_progress(value, fraction * total, total, now=100 + duration * fraction)
    finish_timing(value, now=100 + duration)
    value["state"] = "completed"
    return value


def test_history_is_scaled_by_work_and_filtered_by_device_model_and_state():
    value = job(stage="transcribing", work=120)
    history = [completed("a", 30), completed("b", 40), completed("c", 50),
               completed("different-device", 1, device="cuda")]
    display = public_timing(value, history, now=110)
    assert display["estimate_source"] == "history"
    assert display["eta_seconds"] == 70
    assert display["eta_min_seconds"] == 50
    assert display["eta_max_seconds"] == 90
    assert public_timing(value, history[:2], now=110)["eta_seconds"] is None
    history[1]["state"] = "failed"
    assert public_timing(value, history, now=110)["eta_seconds"] is None


def test_history_is_not_used_for_network_or_overdue_work():
    value = job(stage="transcribing", work=60)
    history = [completed("a", 30), completed("b", 40), completed("c", 50)]
    display = public_timing(value, history, now=141)
    assert display["eta_seconds"] is None
    assert display["timing_status"] == "longer_than_expected"
    value = job(stage="downloading", work=60)
    for previous in history:
        previous["_timing"]["stages"][0]["stage"] = "downloading"
    assert public_timing(value, history, now=101)["eta_seconds"] is None


def test_cached_work_does_not_teach_fresh_jobs_an_instant_processing_speed():
    value = job(stage="transcribing", work=600)
    history = []
    for index in range(3):
        cached = job(f"cached-{index}", "transcribing", 600)
        update_progress(cached, 0, 600, now=100)
        # Even slow disk I/O can take several seconds; elapsed time alone does
        # not demonstrate real transcription throughput.
        update_progress(cached, 600, 600, now=105)
        finish_timing(cached, now=105)
        cached["state"] = "completed"
        history.append(cached)
    assert public_timing(value, history, now=101)["eta_seconds"] is None
    history.extend([completed("real-a", 30), completed("real-b", 40), completed("real-c", 50)])
    display = public_timing(value, history, now=101)
    assert display["estimate_source"] == "history"
    assert display["eta_seconds"] == 399


def test_unmetered_metadata_history_remains_available():
    value, history = job(stage="metadata"), []
    for index, seconds in enumerate((10, 20, 30)):
        previous = job(f"metadata-{index}", "metadata")
        finish_timing(previous, now=100 + seconds)
        previous["state"] = "completed"
        history.append(previous)
    display = public_timing(value, history, now=105)
    assert display["eta_seconds"] == 15
    assert display["estimate_source"] == "history"


def test_finish_timing_is_idempotent_and_timing_is_finite_json_data():
    import json
    value = job(stage="rendering", work=30)
    finish_timing(value, now=125)
    finish_timing(value, now=999)
    assert len(value["_timing"]["stages"]) == 1
    assert public_timing(value, now=999)["elapsed_s"] == 25
    json.dumps(value, allow_nan=False)
    for bad in (float("nan"), float("inf"), True, -1):
        with pytest.raises(ValueError):
            start_stage(value, "rendering", now=bad)
