"""Product-domain records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class Job:
    """A persistent marked-register review job."""

    id: str
    name: str
    workspace_path: Path
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """A source PDF held inside a review job."""

    id: str
    job_id: str
    original_filename: str
    relative_path: Path
    sha256: str
    page_count: int
    polling_district: str | None = None


@dataclass(frozen=True, slots=True)
class LayoutPreview:
    """Detected page geometry and its reviewer-facing overlay."""

    id: str
    source_id: str
    page_number: int
    overlay_relative_path: Path
    image_width: int
    image_height: int
    gutter_x: int
    body_y0: int | None
    body_y1: int | None
    review_status: Literal["pending", "confirmed", "rejected"] = "pending"
    diagnostic: str | None = None
