"""Neutral PDF-to-entry processing pipeline."""

from __future__ import annotations

import base64
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import IO, Literal, Protocol

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
_ANCHOR_RE = re.compile(r"^\d{1,5}(?:/\d{0,2})?$")
_LLAMA_SERVER_START_TIMEOUT_SECONDS = 30 * 60
_RECOGNITION_TIMEOUT_SECONDS = 120


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
        progress: Callable[[int, int], None] | None = None,
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

    def set_processing_state(
        self,
        *,
        job_id: str,
        source_id: str,
        processing_status: str,
        processing_error: str | None = None,
        processed_pages: int | None = None,
    ) -> bool: ...


class ProcessingQueue(Protocol):
    """Serialize resource-heavy local processing tasks."""

    def run(self, task: Callable[[], None]) -> None: ...


class LocalProcessingQueue:
    """Allow only one local inference pipeline to run at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def run(self, task: Callable[[], None]) -> None:
        with self._lock:
            task()


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
        entries = self._engine.extract(
            job=job,
            source=source,
            layouts=layouts,
            progress=lambda completed, _total: self._repository.set_processing_state(
                job_id=job_id,
                source_id=source_id,
                processing_status="running",
                processed_pages=completed,
            ),
        )
        self._repository.replace_register_entries(source_id=source_id, entries=entries)
        return entries

    def queue_source(self, *, job_id: str, source_id: str) -> None:
        source = self._repository.get_source(job_id=job_id, source_id=source_id)
        if source is None:
            raise LookupError("source document not found")
        if source.processing_status in {"queued", "running"}:
            raise ValueError("source processing is already active")
        if not self._repository.set_processing_state(
            job_id=job_id,
            source_id=source_id,
            processing_status="queued",
            processed_pages=0,
        ):
            raise LookupError("source document not found")

    def run_queued_source(self, *, job_id: str, source_id: str) -> None:
        self._repository.set_processing_state(
            job_id=job_id,
            source_id=source_id,
            processing_status="running",
            processed_pages=0,
        )
        try:
            entries = self.process_source(job_id=job_id, source_id=source_id)
            if not entries:
                raise ValueError("no register entries were detected")
        except Exception as error:  # noqa: BLE001 - worker failures must be persisted.
            self._repository.set_processing_state(
                job_id=job_id,
                source_id=source_id,
                processing_status="failed",
                processing_error=str(error) or type(error).__name__,
            )
            return
        source = self._repository.get_source(job_id=job_id, source_id=source_id)
        self._repository.set_processing_state(
            job_id=job_id,
            source_id=source_id,
            processing_status="complete",
            processed_pages=source.page_count if source is not None else 0,
        )

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
    """GLM-OCR adapter for canonical leading-field recognition.

    The model runs in a private llama.cpp server bound to the loopback interface. The server
    starts on first use, downloading the model into the Hugging Face cache if needed, and
    stays loaded until `close()`.
    """

    model_name = "ggml-org/GLM-OCR-GGUF:Q8_0"
    prompt = "Text Recognition:"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._server: _LlamaServer | None = None

    def recognize(self, image: np.ndarray, *, detector_label: str) -> EntryRecognition:
        encoded, png = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
        if not encoded:
            raise OSError("failed to encode OCR input")
        image_url = "data:image/png;base64," + base64.b64encode(png.tobytes()).decode("ascii")
        request = urllib.request.Request(
            f"{self._server_url()}/v1/chat/completions",
            data=json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": image_url}},
                                {"type": "text", "text": self.prompt},
                            ],
                        }
                    ],
                    "max_tokens": 128,
                    "temperature": 0.0,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=_RECOGNITION_TIMEOUT_SECONDS) as response:
            raw_text = json.load(response)["choices"][0]["message"]["content"]
        return parse_entry_recognition(raw_text, detector_label=detector_label)

    def close(self) -> None:
        with self._lock:
            if self._server is not None:
                self._server.stop()
                self._server = None

    def _server_url(self) -> str:
        with self._lock:
            if self._server is None or not self._server.running:
                if self._server is not None:
                    self._server.stop()
                self._server = _LlamaServer.start(self.model_name)
            return self._server.url


@dataclass(frozen=True, slots=True)
class _LlamaServer:
    process: subprocess.Popen[bytes]
    log: IO[bytes]
    url: str

    @classmethod
    def start(cls, model: str) -> _LlamaServer:
        executable = shutil.which("llama-server")
        if executable is None:
            raise RuntimeError("llama-server was not found; install llama.cpp")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # llama.cpp logs timings and model loading at its default verbosity, never OCR text.
        log = TemporaryFile()  # noqa: SIM115 - closed by stop() when the server ends.
        server = cls(
            process=subprocess.Popen(
                [
                    executable,
                    "-hf",
                    model,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--parallel",
                    "1",
                    "--ctx-size",
                    "4096",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            ),
            log=log,
            url=f"http://127.0.0.1:{port}",
        )
        deadline = time.monotonic() + _LLAMA_SERVER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not server.running:
                reason = server.last_log_line()
                server.stop()
                raise RuntimeError(f"llama-server failed to start: {reason}")
            try:
                with urllib.request.urlopen(f"{server.url}/health", timeout=5):
                    return server
            except OSError:
                time.sleep(1)
        server.stop()
        raise RuntimeError("llama-server did not become ready")

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def last_log_line(self) -> str:
        self.log.seek(0)
        lines = self.log.read().decode("utf-8", errors="replace").strip().splitlines()
        return lines[-1] if lines else f"exit code {self.process.returncode}"

    def stop(self) -> None:
        if self.running:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.log.close()


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
        progress: Callable[[int, int], None] | None = None,
    ) -> list[RegisterEntry]:
        if source.polling_district is None:
            raise ValueError("source polling district is not confirmed")
        if not layouts or any(
            layout.review_status not in {"confirmed", "excluded"} for layout in layouts
        ):
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
                if layout.review_status == "excluded":
                    if progress is not None:
                        progress(page_number, len(document))
                    continue
                image = _render_page(page, dpi=self._dpi)
                entries.extend(
                    self._extract_page(
                        job=job,
                        source=source,
                        layout=layout,
                        image=image,
                    )
                )
                if progress is not None:
                    progress(page_number, len(document))
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
            possible_missing_before = _anchors_after_large_gaps(anchors)
            for entry_index, (anchor, crop) in enumerate(
                _entry_crops(image, anchors=anchors, x0=x0, x1=x1, y0=body_y0, y1=body_y1)
            ):
                recognition = self._entry_recognizer.recognize(
                    crop,
                    detector_label=anchor.label,
                )
                status, reason = analyze_register_status(crop)
                warnings = list(recognition.warnings)
                if anchor in possible_missing_before:
                    warnings.append("possible_missing_row_before")
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
    candidates: list[Anchor] = []
    seen_y: set[int] = set()
    for anchor in sorted(anchors, key=lambda item: item.y_center):
        if not _ANCHOR_RE.fullmatch(anchor.label):
            continue
        if not body_y0 <= anchor.y_center <= body_y1 or anchor.y_center in seen_y:
            continue
        candidates.append(anchor)
        seen_y.add(anchor.y_center)
    median_gap = _median_anchor_gap(candidates)
    candidates = _reject_implausibly_close_anchors(candidates, median_gap=median_gap)
    median_gap = _median_anchor_gap(candidates)
    return [
        candidate
        for index, candidate in enumerate(candidates)
        if not _is_isolated_anchor_outlier(index, candidates, median_gap=median_gap)
    ]


def _reject_implausibly_close_anchors(
    anchors: list[Anchor],
    *,
    median_gap: int | None,
) -> list[Anchor]:
    if median_gap is None:
        return anchors
    kept: list[Anchor] = []
    index = 0
    while index < len(anchors):
        group = [anchors[index]]
        index += 1
        while (
            index < len(anchors)
            and anchors[index].y_center - group[-1].y_center < median_gap * 0.5
        ):
            group.append(anchors[index])
            index += 1
        if len(group) == 1:
            kept.append(group[0])
            continue
        chosen = _best_close_anchor(
            group,
            previous_anchor=kept[-1] if kept else None,
            next_anchor=anchors[index] if index < len(anchors) else None,
        )
        if chosen is not None:
            kept.append(chosen)
    return kept


def _best_close_anchor(
    anchors: list[Anchor],
    *,
    previous_anchor: Anchor | None,
    next_anchor: Anchor | None,
) -> Anchor | None:
    scored = [
        (
            _sequence_penalty(
                anchor,
                previous_anchor=previous_anchor,
                next_anchor=next_anchor,
            ),
            -(anchor.confidence or 0),
            anchor.y_center,
            anchor,
        )
        for anchor in anchors
    ]
    best = min(scored)
    return None if best[0] > 1000 else best[3]


def _sequence_penalty(
    anchor: Anchor,
    *,
    previous_anchor: Anchor | None,
    next_anchor: Anchor | None,
) -> int:
    key = _anchor_sort_key(anchor)
    if key is None:
        return 10_000
    penalty = 0
    previous_key = _anchor_sort_key(previous_anchor) if previous_anchor is not None else None
    if previous_key is not None:
        penalty += abs(key[0] - previous_key[0] - 1)
    next_key = _anchor_sort_key(next_anchor) if next_anchor is not None else None
    if next_key is not None:
        penalty += abs(next_key[0] - key[0] - 1)
    return penalty


def _is_isolated_anchor_outlier(
    index: int,
    anchors: list[Anchor],
    *,
    median_gap: int | None,
) -> bool:
    current = _anchor_sort_key(anchors[index])
    if current is None or len(anchors) < 2:
        return False
    neighboring_bases = _neighboring_anchor_bases(index, anchors)
    if len(neighboring_bases) < 2:
        return False
    if not all(abs(current[0] - base) > 1000 for base in neighboring_bases):
        return False
    if median_gap is None:
        return False
    gaps: list[int] = []
    if index > 0:
        gaps.append(anchors[index].y_center - anchors[index - 1].y_center)
    if index + 1 < len(anchors):
        gaps.append(anchors[index + 1].y_center - anchors[index].y_center)
    return any(gap < median_gap * 0.5 or gap > median_gap * 1.5 for gap in gaps)


def _neighboring_anchor_bases(index: int, anchors: list[Anchor]) -> list[int]:
    bases: list[int] = []
    for neighbor_index in (index - 1, index + 1, index - 2, index + 2):
        if not 0 <= neighbor_index < len(anchors):
            continue
        key = _anchor_sort_key(anchors[neighbor_index])
        if key is not None:
            bases.append(key[0])
        if len(bases) == 2:
            break
    return bases


def _median_anchor_gap(anchors: Sequence[Anchor]) -> int | None:
    if len(anchors) < 2:
        return None
    gaps = [
        after.y_center - before.y_center
        for before, after in zip(anchors, anchors[1:], strict=False)
    ]
    return sorted(gaps)[(len(gaps) - 1) // 2]


def _anchors_after_large_gaps(anchors: Sequence[Anchor]) -> set[Anchor]:
    if len(anchors) < 3:
        return set()
    median_gap = _median_anchor_gap(anchors)
    if median_gap is None:
        return set()
    return {
        after
        for before, after in zip(anchors, anchors[1:], strict=False)
        if after.y_center - before.y_center > median_gap * 1.8
    }


def _anchor_sort_key(anchor: Anchor) -> tuple[int, int] | None:
    label = anchor.label[:-1] if anchor.label.endswith("/") else anchor.label
    match = re.fullmatch(r"(?P<base>\d{1,5})(?:/(?P<suffix>\d{1,2}))?", label)
    if match is None:
        return None
    return int(match.group("base")), int(match.group("suffix") or 0)


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
            half_height = max(60, round(image.shape[0] * 0.035))
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
