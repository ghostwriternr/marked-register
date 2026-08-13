"""Persisted page-layout analysis."""

from __future__ import annotations

from collections.abc import Sequence
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Protocol
from uuid import uuid4

import cv2
import numpy as np
import pymupdf

from marked_register.domain import Job, LayoutPreview, SourceDocument


class LayoutRepository(Protocol):
    """Metadata required by the layout workflow."""

    def get(self, job_id: str) -> Job | None: ...

    def get_source(self, *, job_id: str, source_id: str) -> SourceDocument | None: ...

    def replace_layout_previews(
        self,
        *,
        source_id: str,
        previews: Sequence[LayoutPreview],
    ) -> None: ...

    def layout_previews_for_source(self, source_id: str) -> list[LayoutPreview]: ...

    def get_layout_preview(
        self,
        *,
        job_id: str,
        preview_id: str,
    ) -> LayoutPreview | None: ...

    def set_layout_review_status(
        self,
        *,
        source_id: str,
        review_status: Literal["confirmed", "rejected"],
    ) -> int: ...

    def set_layout_preview_review_status(
        self,
        *,
        job_id: str,
        preview_id: str,
        review_status: str,
    ) -> bool: ...


class LayoutOverlayStore(Protocol):
    """Reviewer-facing layout overlay storage."""

    def write(
        self,
        *,
        job: Job,
        source_id: str,
        page_number: int,
        image: np.ndarray,
    ) -> Path: ...


class LocalLayoutOverlayStore:
    """Write overlays below the job's local artifact directory."""

    def write(
        self,
        *,
        job: Job,
        source_id: str,
        page_number: int,
        image: np.ndarray,
    ) -> Path:
        relative_path = (
            Path("artifacts") / "layout" / source_id / f"page_{page_number:04d}.png"
        )
        destination = job.workspace_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        success, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not success:
            raise OSError("failed to encode layout preview")
        with NamedTemporaryFile(dir=destination.parent, prefix=".layout-", delete=False) as file:
            temporary_path = Path(file.name)
            file.write(encoded.tobytes())
        replace(temporary_path, destination)
        return relative_path


class Layouts:
    """Analyze and retrieve page layouts for review."""

    def __init__(self, repository: LayoutRepository, overlays: LayoutOverlayStore) -> None:
        self._repository = repository
        self._overlays = overlays

    def analyze_source(self, *, job_id: str, source_id: str, dpi: int = 150) -> None:
        job = self._repository.get(job_id)
        source = self._repository.get_source(job_id=job_id, source_id=source_id)
        if job is None or source is None:
            raise LookupError("source document not found")
        if source.polling_district is None:
            raise ValueError("confirm the polling district before layout analysis")

        source_path = (job.workspace_path / source.relative_path).resolve()
        if not source_path.is_relative_to(job.workspace_path.resolve()):
            raise ValueError("source path is outside the job workspace")

        previews: list[LayoutPreview] = []
        with pymupdf.open(source_path) as document:
            for page_index, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height,
                    pixmap.width,
                    pixmap.n,
                )
                image = image.copy()
                diagnostics: list[str] = []
                try:
                    gutter_x = detect_gutter_x(image)
                except ValueError as error:
                    gutter_x = image.shape[1] // 2
                    diagnostics.append(str(error))
                try:
                    body_y0, body_y1 = detect_entry_body_bounds(image)
                except ValueError as error:
                    body_y0 = body_y1 = None
                    diagnostics.append(str(error))
                overlay = draw_layout_overlay(
                    image,
                    gutter_x=gutter_x,
                    body_y0=body_y0,
                    body_y1=body_y1,
                )
                relative_path = self._overlays.write(
                    job=job,
                    source_id=source.id,
                    page_number=page_index,
                    image=overlay,
                )
                previews.append(
                    LayoutPreview(
                        id=str(uuid4()),
                        source_id=source.id,
                        page_number=page_index,
                        overlay_relative_path=relative_path,
                        image_width=image.shape[1],
                        image_height=image.shape[0],
                        gutter_x=gutter_x,
                        body_y0=body_y0,
                        body_y1=body_y1,
                        diagnostic="; ".join(diagnostics) or None,
                    )
                )

        self._repository.replace_layout_previews(source_id=source.id, previews=previews)

    def previews_for_source(self, source_id: str) -> list[LayoutPreview]:
        return self._repository.layout_previews_for_source(source_id)

    def preview_path(self, *, job_id: str, preview_id: str) -> Path:
        job = self._repository.get(job_id)
        preview = self._repository.get_layout_preview(job_id=job_id, preview_id=preview_id)
        if job is None or preview is None:
            raise LookupError("layout preview not found")
        path = (job.workspace_path / preview.overlay_relative_path).resolve()
        if not path.is_relative_to(job.workspace_path.resolve()):
            raise ValueError("layout preview is outside the job workspace")
        return path

    def review_source(
        self,
        *,
        job_id: str,
        source_id: str,
        decision: str,
    ) -> None:
        source = self._repository.get_source(job_id=job_id, source_id=source_id)
        if source is None:
            raise LookupError("source document not found")
        if decision not in {"confirmed", "rejected"}:
            raise ValueError("layout decision must be confirmed or rejected")
        previews = self._repository.layout_previews_for_source(source_id)
        if decision == "confirmed" and any(
            preview.diagnostic and preview.review_status != "excluded"
            for preview in previews
        ):
            raise ValueError("resolve layout diagnostics before confirmation")
        updated = self._repository.set_layout_review_status(
            source_id=source_id,
            review_status=decision,
        )
        if updated == 0:
            raise ValueError("analyze page layouts before reviewing them")

    def exclude_preview(self, *, job_id: str, preview_id: str) -> None:
        preview = self._repository.get_layout_preview(
            job_id=job_id,
            preview_id=preview_id,
        )
        if preview is None:
            raise LookupError("layout preview not found")
        if not self._repository.set_layout_preview_review_status(
            job_id=job_id,
            preview_id=preview_id,
            review_status="excluded",
        ):
            raise LookupError("layout preview not found")


def detect_gutter_x(
    image: np.ndarray,
    *,
    threshold: int = 180,
    search_radius_fraction: float = 0.10,
) -> int:
    """Detect the lowest-ink vertical gutter near the page midpoint."""
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    profile = (gray < threshold).mean(axis=0)
    window = max(3, image.shape[1] // 100)
    smoothed = np.convolve(profile, np.ones(window) / window, mode="same")
    midpoint = image.shape[1] // 2
    radius = max(1, int(image.shape[1] * search_radius_fraction))
    start = midpoint - radius
    zone = smoothed[start : midpoint + radius + 1]
    if float(np.ptp(zone)) == 0.0:
        raise ValueError("cannot detect a two-column gutter")
    min_ink = float(np.min(zone))
    candidates = np.flatnonzero(zone <= max(min_ink * 1.35, min_ink + 0.005))
    chosen = min(candidates, key=lambda offset: (abs(start + int(offset) - midpoint), int(offset)))
    return start + int(chosen)


def detect_entry_body_bounds(
    image: np.ndarray,
    *,
    threshold: int = 180,
) -> tuple[int, int]:
    """Detect body bounds from long horizontal rules in expected page zones."""
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    profile = (gray < threshold).mean(axis=1)
    smoothed = np.convolve(profile, np.ones(5) / 5, mode="same")
    for ink_fraction in (0.35, 0.25):
        rows = np.flatnonzero(smoothed >= ink_fraction)
        segments = _contiguous_segments(rows)
        top = [
            segment
            for segment in segments
            if image.shape[0] * 0.10 <= segment[0] <= image.shape[0] * 0.25
        ]
        bottom = [
            segment
            for segment in segments
            if image.shape[0] * 0.85 <= segment[0] <= image.shape[0] * 0.98
        ]
        if top and bottom:
            y0 = max(top, key=lambda segment: segment[1])[1] + 5
            y1 = min(bottom, key=lambda segment: segment[0])[0] - 4
            if y1 > y0:
                return y0, y1
    raise ValueError("entry-body rules were not detected")


def draw_layout_overlay(
    image: np.ndarray,
    *,
    gutter_x: int,
    body_y0: int | None,
    body_y1: int | None,
) -> np.ndarray:
    """Draw detected columns and body bounds on a page image."""
    overlay = image.copy()
    cv2.line(overlay, (gutter_x, 0), (gutter_x, image.shape[0] - 1), (255, 40, 40), 4)
    if body_y0 is not None and body_y1 is not None:
        cv2.rectangle(overlay, (0, body_y0), (gutter_x - 1, body_y1), (30, 180, 80), 3)
        cv2.rectangle(
            overlay,
            (gutter_x, body_y0),
            (image.shape[1] - 1, body_y1),
            (30, 180, 80),
            3,
        )
    return overlay


def _contiguous_segments(values: np.ndarray) -> list[tuple[int, int]]:
    if len(values) == 0:
        return []
    segments: list[tuple[int, int]] = []
    start = previous = int(values[0])
    for value in values[1:]:
        current = int(value)
        if current == previous + 1:
            previous = current
        else:
            segments.append((start, previous))
            start = previous = current
    segments.append((start, previous))
    return segments
