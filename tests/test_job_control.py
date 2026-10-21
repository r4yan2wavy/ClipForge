"""Real process interruption and isolated pipeline progress controls."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from clipforge.common import DB, Settings, run_command, stable_id
from clipforge.job_control import (JobCancelled, controlled,
                                   ffmpeg_progress, job_control, report)
from clipforge.worker import prepare_manifest
from clipforge.web_render import render_manual


def test_cancel_terminates_and_reaps_real_process_before_returning():
    cancelled = threading.Event()
    output = []

    def receive(line):
        output.append(line)
        if line == "ready":
            cancelled.set()

    started = time.monotonic()
    with job_control(cancelled=cancelled.is_set):
        with pytest.raises(JobCancelled):
            run_command([sys.executable, "-c",
                         "import time; print('ready', flush=True); time.sleep(30)"],
                        on_stdout=receive)
    assert output == ["ready"]
    assert time.monotonic() - started < 4
    # The cancellation context was reset even though a command raised.
    assert not controlled()
    assert run_command([sys.executable, "-c", "print('next job')"]).stdout.strip() == "next job"


def test_context_keeps_stdout_stderr_exit_and_timeout_semantics():
    with job_control(cancelled=lambda: False):
        result = run_command([sys.executable, "-c",
                              "import sys; print('hello'); print('diagnostic', file=sys.stderr)"])
        assert result.stdout == "hello\n"
        assert result.stderr == "diagnostic\n"
        with pytest.raises(subprocess.CalledProcessError) as failed:
            run_command([sys.executable, "-c", "import sys; print('details'); sys.exit(7)"])
        assert failed.value.returncode == 7
        assert failed.value.output == "details\n"
        with pytest.raises(subprocess.TimeoutExpired) as timed_out:
            run_command([sys.executable, "-c",
                         "import time; print('started', flush=True); time.sleep(30)"], timeout=.3)
        assert "started" in timed_out.value.output


def test_controls_are_thread_local_and_nested_context_restores_parent():
    observations, progress = [], []
    with job_control(cancelled=lambda: False, on_progress=lambda *args: progress.append(args)):
        # A new thread sees no inherited control. Install a cancelled nested
        # controller in this thread separately to test restoration after error.
        thread = threading.Thread(target=lambda: observations.append(controlled()))
        thread.start()
        thread.join()
        with pytest.raises(JobCancelled):
            with job_control(cancelled=lambda: True):
                pass
        report("selecting", 2, 5)
    assert observations == [False]
    assert progress == [("selecting", 2, 5)]
    assert not controlled()


def test_ffmpeg_times_are_real_seconds_and_invalid_output_is_ignored():
    progress = []
    with job_control(cancelled=lambda: False, on_progress=lambda *args: progress.append(args)):
        receive = ffmpeg_progress(10)
        for line in ("frame=33", "out_time_us=N/A", "out_time_us=2500000", "out_time_us=11000000"):
            receive(line)
    assert progress == [("rendering", 2.5, 10), ("rendering", 10, 10)]


def test_cancelled_engine_job_is_not_misreported_as_input_or_model_failure():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        settings = Settings()
        settings.paths.root = str(root)
        path = root / "never-read.json"
        db = DB(root / "state.sqlite")
        try:
            def cancel_on_report(*args):
                raise JobCancelled()
            with pytest.raises(JobCancelled):
                # Report will cancel before reading any user/model inputs.
                with job_control(cancelled=lambda: False, on_progress=cancel_on_report):
                    prepare_manifest(settings, db, path)
            row = db.conn.execute("SELECT state,error FROM jobs WHERE id=?", (stable_id(str(path.resolve())),)).fetchone()
            assert row["state"] == "CANCELLED"
            assert row["error"] is None
            assert not db.conn.execute("SELECT 1 FROM artifacts").fetchone()
        finally:
            db.close()


def test_real_render_measures_seconds_and_removes_partial_files_on_cancel():
    ffmpeg = os.environ.get("CLIPFORGE_TEST_FFMPEG")
    ffprobe = os.environ.get("CLIPFORGE_TEST_FFPROBE")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg + ffprobe required for real controlled rendering")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "owned.mp4"
        run_command([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                     "testsrc2=size=160x90:rate=12", "-t", "1.5", "-c:v", "libx264", str(source)])
        settings = Settings()
        settings.paths.root = str(root)
        settings.render.ffmpeg, settings.render.ffprobe = ffmpeg, ffprobe
        settings.render.width, settings.render.height, settings.render.fps = 180, 320, 12
        measurements = []
        with job_control(cancelled=lambda: False,
                         on_progress=lambda *values: measurements.append(values)):
            result = render_manual(settings, source, root / "finished", start_s=0, end_s=1,
                                   framing="fit")
        assert Path(result["video_path"]).is_file()
        assert any(stage == "rendering" and completed and completed > .1 and total == 1
                   for stage, completed, total in measurements)
        cancelled = threading.Event()

        def cancel_after_first_frames(stage, completed, total):
            if stage == "rendering" and completed and completed > 0:
                cancelled.set()

        with pytest.raises(JobCancelled):
            with job_control(cancelled=cancelled.is_set, on_progress=cancel_after_first_frames):
                render_manual(settings, source, root / "cancelled", start_s=0, end_s=1,
                              framing="fit")
        assert not (root / "cancelled" / "clip.mp4").exists()
        assert not list((root / "cancelled").glob(".manual-*"))
        assert (root / "finished" / "clip.mp4").is_file()
