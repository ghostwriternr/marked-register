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


RegisterStatus = Literal[
    "voted_in_person",
    "no_in_person_mark",
    "struck_through",
    "unresolved",
]
ReviewOutcome = Literal["confirmed", "corrected", "unresolved", "rejected"]


@dataclass(frozen=True, slots=True)
class RegisterEntry:
    """One predicted elector row awaiting a human review decision."""

    id: str
    source_id: str
    page_number: int
    column: Literal["L", "R"]
    entry_index: int
    crop_relative_path: Path | None
    detector_label: str
    elector_number: str | None
    printed_marker: str | None
    register_status: RegisterStatus
    prediction_reason: str
    recognizer_model: str
    warnings: tuple[str, ...] = ()
    origin: Literal["predicted", "manual"] = "predicted"
    review_outcome: ReviewOutcome | None = None
    reviewed_elector_number: str | None = None
    reviewed_register_status: RegisterStatus | None = None
    review_note: str = ""
