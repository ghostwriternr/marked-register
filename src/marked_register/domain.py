"""Product-domain records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


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
