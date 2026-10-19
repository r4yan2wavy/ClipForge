from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Word(StrictModel):
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def interval(self):
        if self.end_s <= self.start_s:
            raise ValueError("word end must follow start")
        return self


class Span(Word):
    id: str = Field(min_length=1)


class Transcript(StrictModel):
    language: str
    words: list[Word]
    spans: list[Span]
    duration_s: float = Field(gt=0)
    version: str = VERSION

    @model_validator(mode="after")
    def timeline(self):
        if not self.words or not self.spans:
            raise ValueError("empty speech; review source")
        if len({s.id for s in self.spans}) != len(self.spans):
            raise ValueError("duplicate boundary IDs")
        for items in (self.words, self.spans):
            if any(x.end_s > self.duration_s + 0.02 for x in items):
                raise ValueError("transcript exceeds original duration")
            if any(b.start_s < a.start_s for a, b in zip(items, items[1:])):
                raise ValueError("transcript must retain chronological original times")
        return self


class Candidate(StrictModel):
    start_id: str
    end_id: str
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    hook_id: str
    payoff_id: str
    score: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)
    needs_visual_review: bool


class CandidateList(StrictModel):
    clips: list[Candidate] = Field(max_length=20)


class Metadata(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(max_length=30)

    @model_validator(mode="after")
    def clean(self):
        if not self.title.strip() or any(c in self.title for c in "<>\r\n"):
            raise ValueError("invalid title")
        if any(not t.strip() or len(t) > 100 for t in self.tags):
            raise ValueError("invalid tag")
        if sum(len(t) + 3 for t in self.tags) > 500:
            raise ValueError("tag budget exceeded")
        return self


class SourceManifest(StrictModel):
    media_path: str
    source_video_id: str | None = None
    source_url: str | None = None
    rights_category: Literal["original", "permission", "licensed", "public_domain"]
    permission_reference: str = Field(min_length=1)
    creator_credit: str = Field(min_length=1)
    editorial_approved: bool = False
    visual_approved: bool = False
    metadata_approved: bool = False
    contribution: str = Field(min_length=1)
    music_path: str | None = None
    music_license_reference: str | None = None
    music_attribution: str | None = None
    music_catalog: str | None = None
    schedule: str | None = None
    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None
    framing: Literal["center_crop", "fit", "blur"] = "center_crop"
    transcript_path: str | None = None
    candidates_path: str | None = None
    metadata_path: str | None = None

    @model_validator(mode="after")
    def music_rights(self):
        if not all(s.strip() for s in (self.permission_reference, self.creator_credit, self.contribution)):
            raise ValueError("rights, credit and contribution records cannot be blank")
        if self.music_path and not self.music_license_reference:
            raise ValueError("music requires a verified license reference")
        return self


class PathsConfig(StrictModel):
    root: str = "."
    state: str = "data/state.sqlite3"
    inbox: str = "inbox"
    prepared: str = "prepared"
    cache: str = "data/cache"
    logs: str = "data/logs"


class SpeechConfig(StrictModel):
    model: str = "models/faster-whisper-small"
    device: Literal["cpu", "cuda"] = "cpu"
    compute_type: str = "int8"
    language: str | None = "en"
    cpu_threads: int = Field(default=4, ge=1)
    beam_size: int = Field(default=3, ge=1, le=10)


class BrainConfig(StrictModel):
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b-instruct"
    context_tokens: int = Field(default=8192, ge=4096)
    window_chars: int = Field(default=7000, ge=1000, le=15000)
    overlap_spans: int = Field(default=5, ge=1, le=20)
    min_clip_s: float = Field(default=20.0, ge=1)
    max_clip_s: float = Field(default=60.0, gt=1)
    max_clips: int = Field(default=3, ge=1, le=10)
    correction_retries: int = Field(default=1, ge=0, le=2)
    timeout_s: int = Field(default=600, ge=10)

    @model_validator(mode="after")
    def bounds(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.ollama_url)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username:
            raise ValueError("Ollama must use a local loopback HTTP endpoint")
        if ":cloud" in self.model.lower() or "-cloud" in self.model.lower():
            raise ValueError("hosted Ollama models are forbidden")
        if self.max_clip_s < self.min_clip_s:
            raise ValueError("clip maximum smaller than minimum")
        # Conservative bound: UTF-8 bytes can approach token count for some languages.
        if self.window_chars > (self.context_tokens - 2200) * 2:
            raise ValueError("window too large for context; reduce window_chars")
        return self


class RenderConfig(StrictModel):
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    width: int = Field(default=1080, ge=2)
    height: int = Field(default=1920, ge=2)
    fps: int = Field(default=30, ge=1, le=60)
    crf: int = Field(default=21, ge=0, le=40)
    preset: str = "veryfast"
    caption_format: Literal["srt", "ass"] = "ass"
    font: str = "Arial"
    font_size: int = Field(default=64, ge=10, le=150)
    margin_v: int = Field(default=230, ge=0)
    loudness_lufs: float = Field(default=-16.0, ge=-30, le=-5)
    music_gain_db: float = Field(default=-24.0, ge=-60, le=0)
    ducking: bool = True

    @model_validator(mode="after")
    def even_dimensions(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("H.264 yuv420p dimensions must be even")
        return self


class DiscoveryConfig(StrictModel):
    api_key_env: str = "YOUTUBE_API_KEY"
    channel_ids: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    search_every_hours: int = Field(default=24, ge=1)
    monitor_every_minutes: int = Field(default=60, ge=1)
    max_pages: int = Field(default=2, ge=1, le=20)
    page_size: int = Field(default=50, ge=1, le=50)
    daily_budget_units: int = Field(default=500, ge=1)
    search_daily_budget: int = Field(default=20, ge=1)
    search_call_cost: int = Field(default=1, ge=1)
    general_read_call_cost: int = Field(default=1, ge=1)
    min_duration_s: int = 300
    max_duration_s: int = 14400
    max_age_days: int = 30
    language: str | None = "en"
    cache_minutes: int = 30
    retention_days: int = Field(default=28, ge=1, le=30)
    derived_metrics_enabled: bool = False
    approval_reference: str | None = None
    velocity_floor: float = Field(default=1.0, gt=0)
    min_subscribers: int = Field(default=1000, ge=1)
    confirmation_count: int = Field(default=3, ge=2)
    threshold_velocity: float = Field(default=100.0, ge=0)
    threshold_lift: float = Field(default=1.5, ge=0)
    min_observation_minutes: float = Field(default=15.0, gt=0)
    baseline_min_samples: int = Field(default=5, ge=1)
    baseline_age_tolerance_hours: float = Field(default=2.0, ge=0)
    baseline_ratio_threshold: float = Field(default=1.5, ge=0)
    max_confirmation_gap_hours: float = Field(default=3.0, gt=0)


class UploadConfig(StrictModel):
    enabled: bool = False
    target_channel_id: str = ""
    client_secrets: str = "secrets/client_secret.json"
    token_file: str = "secrets/token.json"
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    made_for_kids: bool | None = None
    contains_synthetic_media: bool | None = None
    min_lead_minutes: int = Field(default=60, ge=1)
    chunk_bytes: int = Field(default=8388608, ge=262144)
    max_retries: int = Field(default=3, ge=0, le=6)
    request_timeout_s: int = Field(default=60, ge=1)
    poll_every_minutes: int = Field(default=15, ge=1)


class Settings(StrictModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    timezone: str = "America/New_York"
    max_job_attempts: int = Field(default=3, ge=1, le=10)

    def resolve(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (Path(self.paths.root) / path).resolve()


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path).resolve()
    result = Settings.model_validate_json(config_path.read_text(encoding="utf-8"))
    result.paths.root = str((config_path.parent / result.paths.root).resolve())
    return result


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def run_command(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    # Never use shell=True. Do not include user/AI strings in executable code.
    return subprocess.run([str(a) for a in args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kwargs)


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, manifest_path TEXT NOT NULL, input_hash TEXT,
              state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (
              clip_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, artifact_dir TEXT NOT NULL,
              render_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT, entity_id TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS slots (
              channel_id TEXT NOT NULL, publish_at TEXT NOT NULL, clip_id TEXT NOT NULL,
              PRIMARY KEY(channel_id,publish_at), UNIQUE(channel_id,clip_id));
        """)
        if "active" not in {r[1] for r in self.conn.execute("PRAGMA table_info(artifacts)")}:
            self.conn.execute("ALTER TABLE artifacts ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

    @contextlib.contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def set_job(self, job_id: str, state: str, error: str | None = None):
        self.conn.execute("UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?",
                          (state, error, iso_now(), job_id))

    def event(self, kind: str, entity_id: str, detail: Any):
        # Callers pass categories and safe IDs, never HTTP bodies, credentials, session URIs.
        self.conn.execute("INSERT INTO events(at,kind,entity_id,detail) VALUES(?,?,?,?)",
                          (iso_now(), kind, entity_id, json.dumps(detail, default=str)))

    def close(self):
        self.conn.close()
