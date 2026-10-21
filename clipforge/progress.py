"""Measured, stage-scoped timing for the local studio's persistent jobs.

Private timing state is JSON-serializable so a completed run can inform a later
one. An estimate is deliberately absent until there is enough evidence; queue
time, estimated stage percentages, and unknown download sizes are not clocks.
"""
from __future__ import annotations

import math
import statistics
import time
from collections.abc import Iterable


HISTORY_STAGES = {"transcribing", "selecting", "rendering", "metadata", "verifying", "merging"}
METERED_STAGES = {"transcribing", "selecting", "rendering"}
MIN_SAMPLE_SECONDS = 2.0
MIN_SAMPLES = 3
MAX_SAMPLES = 24


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value < 0 or (positive and value == 0):
        return None
    return float(value)


def _clock(now):
    value = time.time() if now is None else _number(now)
    if value is None:
        raise ValueError("Job timing requires a finite nonnegative timestamp.")
    return value


def _work_total(job, stage):
    totals = job.get("stage_work_totals", {})
    return _number(totals.get(stage), positive=True) if isinstance(totals, dict) else None


def _close_stage(timing, now):
    if timing.get("stage") is not None:
        samples = timing.get("samples", [])
        metered = bool(samples or timing.get("total") or timing.get("work_total")) or timing["stage"] in METERED_STAGES
        # A cache hit often emits only 0 -> total. Its file-read duration is not
        # evidence of how quickly a fresh transcription or render will execute.
        has_rate_evidence = (not timing.get("invalid_counters") and len(samples) >= MIN_SAMPLES
                             and samples[-1][0] - samples[0][0] >= MIN_SAMPLE_SECONDS
                             and samples[-1][1] > samples[0][1])
        timing["stages"].append({
            "stage": timing["stage"],
            "duration_s": max(0.0, now - timing["stage_started_at"]),
            "work_total": timing.get("total") or timing.get("work_total"),
            "metered": metered,
            "has_rate_evidence": bool(has_rate_evidence),
        })
        timing["stages"] = timing["stages"][-100:]


def start_stage(job: dict, stage: str, now=None) -> None:
    """Begin a stage, preserving measurements on repeated stage notifications.

    Set ``job['stage_work_totals'][stage]`` to its expected work (source seconds,
    clip seconds, or bytes) when known. Set ``job['timing_profile']`` to the
    device/model/settings identity used to match completed historical jobs.
    A queued stage or a previously finished run starts a new timing attempt.
    """
    now = _clock(now)
    if stage == "queued":
        job.pop("_timing", None)
        return
    timing = job.get("_timing")
    if not isinstance(timing, dict) or timing.get("finished_at") is not None:
        timing = {"started_at": now, "finished_at": None, "stages": [], "stage": None}
        job["_timing"] = timing
    if timing.get("stage") == stage:
        timing["last_at"] = now
        return
    _close_stage(timing, now)
    timing.update(stage=stage, stage_started_at=now, last_at=now,
                  work_total=_work_total(job, stage), completed=None, total=None,
                  samples=[], last_change_at=None, invalid_counters=False)


def update_progress(job: dict, completed, total, now=None) -> None:
    """Record cumulative work within the current stage.

    Download counters can restart for separate streams or include wire overhead.
    A reset or an impossible count invalidates that stage's counter estimate;
    stage transitions create a clean measurement window.
    """
    now = _clock(now)
    timing = job.get("_timing")
    if not isinstance(timing, dict) or timing.get("finished_at") is not None:
        return
    timing["last_at"] = now
    amount, size = _number(completed), _number(total, positive=True)
    previous = timing.get("completed")
    if (amount is None or (previous is not None and amount < previous)
            or (size is not None and amount > size)):
        timing.update(invalid_counters=True, samples=[], completed=None, total=None)
        return
    timing.update(completed=amount, total=size)
    if timing["invalid_counters"]:
        return
    samples = timing["samples"]
    if samples and now < samples[-1][0]:
        timing.update(invalid_counters=True, samples=[])
        return
    # Repeated polling does not manufacture throughput or extend a stalled rate.
    if samples and amount == samples[-1][1]:
        return
    if samples and now == samples[-1][0]:
        samples[-1][1] = amount
    else:
        samples.append([now, amount])
    timing["samples"] = samples[-MAX_SAMPLES:]
    timing["last_change_at"] = now


def finish_timing(job: dict, now=None) -> None:
    """Freeze active time and completed stage observations, once per attempt."""
    now = _clock(now)
    timing = job.get("_timing")
    if isinstance(timing, dict) and timing.get("finished_at") is None:
        _close_stage(timing, now)
        timing.update(finished_at=now, last_at=now)


def _history_estimate(job, timing, history: Iterable[dict], elapsed):
    stage = timing["stage"]
    if stage not in HISTORY_STAGES or not job.get("timing_profile"):
        return None
    expected_work = timing.get("total") or timing.get("work_total")
    predictions, job_ids = [], set()
    for previous in history:
        if (previous.get("id") == job.get("id") or previous.get("state") != "completed"
                or previous.get("kind") != job.get("kind")
                or previous.get("mode") != job.get("mode")
                or previous.get("timing_profile") != job.get("timing_profile")):
            continue
        measured = previous.get("_timing", {})
        if not isinstance(measured, dict) or measured.get("finished_at") is None:
            continue
        for observation in measured.get("stages", []):
            if observation.get("stage") != stage:
                continue
            duration = _number(observation.get("duration_s"), positive=True)
            work = _number(observation.get("work_total"), positive=True)
            metered = observation.get("metered", stage in METERED_STAGES or work is not None)
            if metered and observation.get("has_rate_evidence") is not True:
                continue
            if duration is None or bool(expected_work) != bool(work):
                continue
            predicted = duration * expected_work / work if work else duration
            if not math.isfinite(predicted):
                continue
            predictions.append(predicted)
            job_ids.add(previous.get("id"))
    if len(predictions) < 3 or len(job_ids) < 2:
        return None
    middle = statistics.median(predictions)
    if elapsed >= middle:
        return {"timing_status": "longer_than_expected"}
    return {"eta_seconds": math.ceil(middle - elapsed),
            "eta_min_seconds": math.ceil(max(0, min(predictions) - elapsed)),
            "eta_max_seconds": math.ceil(max(predictions) - elapsed),
            "estimate_source": "history", "timing_status": "estimating"}


def public_timing(job: dict, history: Iterable[dict] = (), now=None) -> dict:
    """Return display-safe stage estimates without exposing measurements.

    ``eta_scope`` is always ``stage``: a transcription ETA is not a promise of
    when every clip will finish. Terminal jobs never keep counting upward after
    a restart, even when an interrupted process did not freeze its timing state.
    """
    now = _clock(now)
    result = {"elapsed_s": 0, "stage_elapsed_s": 0, "eta_seconds": None,
              "eta_min_seconds": None, "eta_max_seconds": None,
              "eta_scope": "stage", "estimate_source": None,
              "stage_percent": None, "timing_status": "measuring"}
    state, timing = job.get("state"), job.get("_timing")
    if state == "queued":
        result["timing_status"] = "queued"
        return result
    if not isinstance(timing, dict) or not timing.get("stage"):
        return result
    endpoint = timing.get("finished_at")
    if endpoint is None:
        endpoint = now if state == "running" else timing.get("last_at", timing["started_at"])
    elapsed = max(0.0, endpoint - timing["stage_started_at"])
    result.update(elapsed_s=round(max(0.0, endpoint - timing["started_at"])),
                  stage_elapsed_s=round(elapsed))
    if state != "running" or timing.get("finished_at") is not None:
        result["timing_status"] = "complete"
        return result
    if timing.get("invalid_counters"):
        return result
    completed, total = timing.get("completed"), timing.get("total")
    if completed is not None and total is not None and 0 <= completed <= total:
        result["stage_percent"] = round(100 * completed / total, 1)
    samples = timing.get("samples", [])
    if len(samples) >= MIN_SAMPLES and samples[-1][0] - samples[0][0] >= MIN_SAMPLE_SECONDS:
        intervals = [b[0] - a[0] for a, b in zip(samples, samples[1:]) if b[0] > a[0]]
        stall_after = max(15.0, 3 * statistics.median(intervals))
        if now - samples[-1][0] > stall_after:
            result["timing_status"] = "stalled"
            return result
        rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(samples, samples[1:])
                 if b[0] > a[0] and b[1] > a[1]]
        if rates and total is not None and completed is not None:
            remaining = total - completed
            rates = [rate for rate in rates if math.isfinite(rate) and rate > 0]
            if rates:
                estimates = [remaining / rate for rate in rates]
                if all(math.isfinite(estimate) for estimate in estimates):
                    result.update(eta_seconds=math.ceil(statistics.median(estimates)),
                                  eta_min_seconds=math.ceil(min(estimates)),
                                  eta_max_seconds=math.ceil(max(estimates)),
                                  estimate_source="measured", timing_status="estimating")
                    return result
    historical = _history_estimate(job, timing, history, elapsed)
    if historical:
        result.update(historical)
    return result
