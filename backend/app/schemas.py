"""Pydantic request/response schemas for the downloads API (plan step 8)."""

from datetime import datetime

from pydantic import BaseModel, Field


class ProbeRequest(BaseModel):
    url: str


class QualityOption(BaseModel):
    format_id: str
    ext: str
    resolution: str | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    filesize_approx: int | None = None
    tbr: float | None = None
    # Live rooms carry no resolution/fps/tbr at all — the tier name ('原画',
    # '高清') and the protocol are the only things that tell two otherwise
    # identical 'source-N' entries apart, so both reach the dropdown.
    format_note: str | None = None
    protocol: str | None = None


class ProbeResult(BaseModel):
    platform: str
    title: str | None = None
    duration: float | None = None
    formats: list[QualityOption] = []


class DownloadJobCreate(BaseModel):
    url: str
    format_id: str | None = None
    kind: str = Field(default="video", pattern="^(video|images|story)$")
    audio_only: bool = False
    download_subs: bool = True
    redownload: bool = False


class DownloadJobOut(BaseModel):
    id: int
    url: str
    platform: str
    kind: str
    title: str
    creator: str
    format_id: str | None
    selected_quality: str | None
    status: str
    progress: float
    output_path: str | None
    error: str | None
    is_auto: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
