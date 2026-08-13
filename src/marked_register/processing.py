"""Neutral PDF-to-entry processing pipeline."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Protocol

import cv2
import numpy as np
import pymupdf

from marked_register.domain import (
    Job,
    LayoutPreview,
    RegisterEntry,
    RegisterStatus,
    SourceDocument,
)

_ELECTOR_RE = re.compile(r"^\s*(?P<elector>\d{1,5}(?:/\d{1,2})?)(?:\s+|[-–—]+|(?=[A-Za-z]))")
_MARKER_RE = re.compile(r"^(?:\d{1,5}(?:/\d{1,2})?)\s*(?P<marker>@A|[A-Z]{1,3})?\b")
_ANCHOR_RE = re.compile(r"^\d{1,5}(?:/\d{1,2})?$")


@dataclass(frozen=True, slots=True)
class Anchor:
    """Noisy elector-number geometry used only to bound an entry crop."""

    label: str
    y_center: int
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class EntryRecognition:
    """Reliable leading fields transcribed from an entry crop."""

    elector_number: str | None
    printed_marker: str | None
    warnings: tuple[str, ...] = ()


class AnchorDetector(Protocol):
    """Locate elector-number rows inside one page column."""

    def detect(
        self,
        image: np.ndarray,
        *,
        column: str,
        bbox: tuple[int, int, int, int],
    ) -> list[Anchor]: ...


class EntryRecognizer(Protocol):
    """Transcribe leading elector fields from one row crop."""

    model_name: str

    def recognize(self, image: np.ndarray, *, detector_label: str) -> EntryRecognition: ...


class RegisterEngine(Protocol):
    """Neutral processing interface used by the application workflow."""

    def extract(
        self,
        *,
        job: Job,
        source: SourceDocument,
        layouts: Sequence[LayoutPreview],
    ) -> list[RegisterEntry]: ...


class ProcessingRepository(Protocol):
    """Metadata required to run and persist register processing."""

    def get(self, job_id: str) -> Job | None: ...

    def get_source(self, *, job_id: str, source_id: str) -> SourceDocument | None: ...

    def layout_previews_for_source(self, source_id: str) -> list[LayoutPreview]: ...

    def replace_register_entries(
        self,
        *,
        source_id: str,
        entries: Sequence[RegisterEntry],
    ) -> None: ...

    def register_entries_for_source(self, source_id: str) -> list[RegisterEntry]: ...


class Processing:
    """Persist neutral predictions produced by a register engine."""

    def __init__(self, repository: ProcessingRepository, engine: RegisterEngine) -> None:
        self._repository = repository
        self._engine = engine

    def process_source(self, *, job_id: str, source_id: str) -> list[RegisterEntry]:
        job = self._repository.get(job_id)
        source = self._repository.get_source(job_id=job_id, source_id=source_id)
        if job is None or source is None:
            raise LookupError("source document not found")
        layouts = self._repository.layout_previews_for_source(source_id)
        entries = self._engine.extract(job=job, source=source, layouts=layouts)
        self._repository.replace_register_entries(source_id=source_id, entries=entries)
        return entries

    def entries_for_source(self, source_id: str) -> list[RegisterEntry]:
        return self._repository.register_entries_for_source(source_id)


class TesseractAnchorDetector:
    """Tesseract adapter for noisy structural anchor detection."""

    def detect(
        self,
        image: np.ndarray,
        *,
        column: str,
        bbox: tuple[int, int, int, int],
    ) -> list[Anchor]:
        del column
        import pytesseract

        x0, y0, x1, y1 = bbox
        crop = image[y0:y1, x0:x1]
        data = pytesseract.image_to_data(
            crop,
            config="--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789/",
            output_type=pytesseract.Output.DICT,
        )
        anchors: list[Anchor] = []
        for text, confidence, top, height in zip(
            data["text"],
            data["conf"],
            data["top"],
            data["height"],
            strict=True,
        ):
            label = str(text).strip()
            if not _ANCHOR_RE.fullmatch(label):
                continue
            parsed_confidence = float(confidence)
            anchors.append(
                Anchor(
                    label=label,
                    y_center=y0 + int(top) + int(height) // 2,
                    confidence=parsed_confidence if parsed_confidence >= 0 else None,
                )
            )
        return anchors


class GlmEntryRecognizer:
    """MLX GLM-OCR adapter for canonical leading-field recognition."""

    model_name = "mlx-community/GLM-OCR-8bit"
    prompt = "Text Recognition:"

    def recognize(self, image: np.ndarray, *, detector_label: str) -> EntryRecognition:
        with NamedTemporaryFile(
            suffix=".png",
            prefix="marked-register-ocr-",
            delete=False,
        ) as file:
            path = Path(file.name)
        try:
            if not cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
                raise OSError("failed to write OCR input")
            raw_text = _recognize_image_file(self.model_name, self.prompt, path)
        finally:
            path.unlink(missing_ok=True)
        return parse_entry_recognition(raw_text, detector_label=detector_label)


class LocalRegisterEngine:
    """Turn confirmed local PDF geometry into neutral entry predictions."""

    def __init__(
        self,
        *,
        anchor_detector: AnchorDetector,
        entry_recognizer: EntryRecognizer,
        dpi: int = 300,
    ) -> None:
        self._anchor_detector = anchor_detector
        self._entry_recognizer = entry_recognizer
        self._dpi = dpi

    def extract(
        self,
        *,
        job: Job,
        source: SourceDocument,
        layouts: Sequence[LayoutPreview],
    ) -> list[RegisterEntry]:
        if source.polling_district is None:
            raise ValueError("source polling district is not confirmed")
        if not layouts or any(layout.review_status != "confirmed" for layout in layouts):
            raise ValueError("all page layouts must be confirmed before processing")

        source_path = (job.workspace_path / source.relative_path).resolve()
        if not source_path.is_relative_to(job.workspace_path.resolve()):
            raise ValueError("source path is outside the job workspace")

        entries: list[RegisterEntry] = []
        layouts_by_page = {layout.page_number: layout for layout in layouts}
        with pymupdf.open(source_path) as document:
            for page_number, page in enumerate(document, start=1):
                layout = layouts_by_page.get(page_number)
                if layout is None:
                    raise ValueError(f"confirmed layout missing for page {page_number}")
                image = _render_page(page, dpi=self._dpi)
                entries.extend(
                    self._extract_page(
                        job=job,
                        source=source,
                        layout=layout,
                        image=image,
                    )
                )
        return entries

    def _extract_page(
        self,
        *,
        job: Job,
        source: SourceDocument,
        layout: LayoutPreview,
        image: np.ndarray,
    ) -> list[RegisterEntry]:
        scale_x = image.shape[1] / layout.image_width
        scale_y = image.shape[0] / layout.image_height
        gutter_x = round(layout.gutter_x * scale_x)
        body_y0 = round((layout.body_y0 or 0) * scale_y)
        body_y1 = round((layout.body_y1 or layout.image_height) * scale_y)
        columns: list[tuple[Literal["L", "R"], int, int]] = [
            ("L", 0, gutter_x),
            ("R", gutter_x, image.shape[1]),
        ]

        entries: list[RegisterEntry] = []
        for column, x0, x1 in columns:
            band_width = max(80, round((x1 - x0) * 0.30))
            anchors = self._anchor_detector.detect(
                image,
                column=column,
                bbox=(x0, body_y0, min(x1, x0 + band_width), body_y1),
            )
            anchors = _select_anchors(anchors, body_y0=body_y0, body_y1=body_y1)
            for entry_index, (anchor, crop) in enumerate(
                _entry_crops(image, anchors=anchors, x0=x0, x1=x1, y0=body_y0, y1=body_y1)
            ):
                recognition = self._entry_recognizer.recognize(
                    crop,
                    detector_label=anchor.label,
                )
                status, reason = analyze_register_status(crop)
                warnings = list(recognition.warnings)
                if recognition.elector_number is None:
                    status = "unresolved"
                    reason = "elector_number_not_recognized"
                    warnings.append("elector_number_missing")
                elif recognition.elector_number != anchor.label:
                    warnings.append("detector_label_mismatch")

                entry_id = (
                    f"{source.id}-p{layout.page_number:04d}-{column}-{entry_index:04d}"
                )
                relative_crop = (
                    Path("artifacts") / "entries" / source.id / f"{entry_id}.png"
                )
                _write_crop(job.workspace_path / relative_crop, crop)
                entries.append(
                    RegisterEntry(
                        id=entry_id,
                        source_id=source.id,
                        page_number=layout.page_number,
                        column=column,
                        entry_index=entry_index,
                        crop_relative_path=relative_crop,
                        detector_label=anchor.label,
                        elector_number=recognition.elector_number,
                        printed_marker=recognition.printed_marker,
                        register_status=status,
                        prediction_reason=reason,
                        recognizer_model=self._entry_recognizer.model_name,
                        warnings=tuple(dict.fromkeys(warnings)),
                    )
                )
        return entries


def parse_entry_recognition(raw_text: str, *, detector_label: str) -> EntryRecognition:
    """Parse only reliable leading elector and marker fields from OCR text."""
    selected = raw_text.strip()
    for line in raw_text.splitlines():
        if line.strip().startswith(detector_label):
            selected = line.strip()
            break
    elector_match = _ELECTOR_RE.match(selected)
    if elector_match is None:
        return EntryRecognition(None, None, ("elector_number_missing",))
    elector_number = elector_match.group("elector")
    marker_match = _MARKER_RE.match(selected)
    marker = marker_match.group("marker") if marker_match is not None else None
    return EntryRecognition(elector_number, marker)


def analyze_register_status(image: np.ndarray) -> tuple[RegisterStatus, str]:
    """Classify visible vote marks independently of OCR text."""
    if _horizontal_mark_present(
        image,
        search=(0.06, 0.45),
        kernel_width=15,
        min_width=35,
        max_width_fraction=0.18,
        max_height=25,
        min_aspect=3.0,
    ):
        return "voted_in_person", "dash_present"
    if _horizontal_mark_present(
        image,
        search=(0.20, 0.80),
        kernel_width=35,
        min_width=90,
        max_width_fraction=0.70,
        max_height=20,
        min_aspect=6.0,
    ):
        return "struck_through", "strikethrough_present"
    return "no_in_person_mark", "no_visual_mark"


def _horizontal_mark_present(
    image: np.ndarray,
    *,
    search: tuple[float, float],
    kernel_width: int,
    min_width: int,
    max_width_fraction: float,
    max_height: int,
    min_aspect: float,
) -> bool:
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    mask = (gray < 190).astype(np.uint8)
    x0 = int(mask.shape[1] * search[0])
    x1 = int(mask.shape[1] * search[1])
    region = mask[:, x0:x1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 1))
    horizontal = cv2.morphologyEx(region, cv2.MORPH_OPEN, kernel)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(horizontal, 8)
    max_width = max(min_width, int(mask.shape[1] * max_width_fraction))
    for label in range(1, count):
        _x, _y, width, height, area = stats[label]
        if not min_width <= width <= max_width or height > max_height:
            continue
        if width / max(height, 1) < min_aspect:
            continue
        if area / (width * height) < 0.25:
            continue
        return True
    return False


def _select_anchors(anchors: Sequence[Anchor], *, body_y0: int, body_y1: int) -> list[Anchor]:
    selected: list[Anchor] = []
    seen_y: set[int] = set()
    for anchor in sorted(anchors, key=lambda item: item.y_center):
        if not _ANCHOR_RE.fullmatch(anchor.label):
            continue
        if not body_y0 <= anchor.y_center <= body_y1 or anchor.y_center in seen_y:
            continue
        selected.append(anchor)
        seen_y.add(anchor.y_center)
    return selected


def _entry_crops(
    image: np.ndarray,
    *,
    anchors: Sequence[Anchor],
    x0: int,
    x1: int,
    y0: int,
    y1: int,
):  # type: ignore[no-untyped-def]
    for index, anchor in enumerate(anchors):
        if len(anchors) == 1:
            half_height = max(40, round(image.shape[0] * 0.018))
            crop_y0 = max(y0, anchor.y_center - half_height)
            crop_y1 = min(y1, anchor.y_center + half_height)
        else:
            crop_y0 = (
                max(y0, anchor.y_center - (anchors[1].y_center - anchor.y_center) // 2)
                if index == 0
                else (anchors[index - 1].y_center + anchor.y_center) // 2
            )
            crop_y1 = (
                min(y1, anchor.y_center + (anchor.y_center - anchors[index - 1].y_center) // 2)
                if index == len(anchors) - 1
                else (anchor.y_center + anchors[index + 1].y_center) // 2
            )
        yield anchor, image[crop_y0:crop_y1, x0:x1].copy()


def _render_page(page: pymupdf.Page, *, dpi: int) -> np.ndarray:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height,
        pixmap.width,
        pixmap.n,
    ).copy()


def _write_crop(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
        raise OSError(f"failed to write entry crop: {path}")


@lru_cache(maxsize=2)
def _load_mlx_model(model_name: str):  # type: ignore[no-untyped-def]
    try:
        from mlx_vlm import load
    except ImportError as error:
        raise RuntimeError(
            "local AI dependencies are missing; run `uv sync --extra local-ai`"
        ) from error
    return load(model_name)


def _recognize_image_file(model_name: str, prompt: str, image_path: Path) -> str:
    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
    except ImportError as error:
        raise RuntimeError(
            "local AI dependencies are missing; run `uv sync --extra local-ai`"
        ) from error
    model, processor = _load_mlx_model(model_name)
    formatted_prompt = apply_chat_template(processor, model.config, prompt, num_images=1)
    result = generate(
        model,
        processor,
        formatted_prompt,
        image=[os.fspath(image_path)],
        max_tokens=128,
        temperature=0.0,
        repetition_penalty=1.2,
        verbose=False,
    )
    return result.text
