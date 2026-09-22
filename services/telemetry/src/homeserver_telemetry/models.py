from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceState = Literal["ok", "stale", "unknown", "error"]


def source_status(
    *, last_success_age: float | None, last_error: str | None, stale_after: float = 30
) -> SourceState:
    if last_error:
        return "error"
    if last_success_age is None:
        return "unknown"
    return "stale" if last_success_age > stale_after else "ok"


class SourceSnapshot(BaseModel):
    status: SourceState
    age_seconds: float | None = Field(default=None, ge=0)


class HostSnapshot(BaseModel):
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    ram_percent: float | None = Field(default=None, ge=0, le=100)
    cpu_celsius: float | None = None
    uptime_seconds: int | None = Field(default=None, ge=0)


class CapacitySnapshot(BaseModel):
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    reserved_unallocated_bytes: int = Field(ge=0)
    admissible_bytes: int = Field(ge=0)


class TransferSnapshot(BaseModel):
    download_bps: int = Field(ge=0)
    upload_bps: int = Field(ge=0)
    active_count: int = Field(ge=0)
    queue_count: int = Field(ge=0)


class DownloadSnapshot(BaseModel):
    id: str
    title: str
    progress: float | None = Field(default=None, ge=0, le=1)
    download_bps: int = Field(ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    state: str


class PlaybackItem(BaseModel):
    client: str
    mode: Literal["direct", "transcoding", "unknown"]
    remote: bool | None = None


class PlaybackSnapshot(BaseModel):
    active_count: int | None = Field(default=None, ge=0)
    remote_count: int | None = Field(default=None, ge=0)
    items: list[PlaybackItem] = Field(default_factory=list)


class AlertSnapshot(BaseModel):
    id: str
    severity: Literal["info", "warning", "critical"]
    message: str


class TelemetrySnapshot(BaseModel):
    schema_version: Literal[1]
    sequence: int = Field(ge=0)
    generated_at: datetime
    sources: dict[str, SourceSnapshot]
    host: HostSnapshot
    capacity: CapacitySnapshot
    transfers: TransferSnapshot
    downloads: list[DownloadSnapshot]
    playback: PlaybackSnapshot
    alerts: list[AlertSnapshot]
