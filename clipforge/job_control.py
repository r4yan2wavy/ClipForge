"""Cooperative, context-local cancellation and work measurements for the studio.

CLI jobs have no controller. A studio worker installs one for a single task;
the callbacks never leak into other worker threads or later CLI commands.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable


class JobCancelled(RuntimeError):
    """The operator stopped this task; it is neither a failure nor a retry."""


@dataclass(frozen=True)
class _Control:
    cancelled: Callable[[], bool]
    on_progress: Callable[[str, float | None, float | None], None] | None


_current: ContextVar[_Control | None] = ContextVar("clipforge_job_control", default=None)


@contextmanager
def job_control(*, cancelled: Callable[[], bool], on_progress=None):
    token = _current.set(_Control(cancelled, on_progress))
    try:
        checkpoint()
        yield
    finally:
        _current.reset(token)


def controlled() -> bool:
    return _current.get() is not None


def checkpoint():
    control = _current.get()
    if control is not None and control.cancelled():
        raise JobCancelled("Task cancelled. Completed clips are kept.")


def report(stage: str, completed: float | None = None, total: float | None = None):
    checkpoint()
    control = _current.get()
    if control is not None and control.on_progress is not None:
        control.on_progress(stage, completed, total)
    checkpoint()


def ffmpeg_progress(duration: float, stage: str = "rendering"):
    """Consume FFmpeg's machine-readable output in presentation-time seconds."""
    def receive(line: str):
        if line.startswith("out_time_us="):
            try:
                elapsed = int(line.partition("=")[2]) / 1_000_000
            except ValueError:
                return
            report(stage, max(0.0, min(duration, elapsed)), duration)
    return receive
