from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from marked_register.domain import Job, LayoutPreview, SourceDocument
from marked_register.processing import (
    Anchor,
    EntryRecognition,
    LocalRegisterEngine,
)


class SyntheticAnchorDetector:
    def detect(
        self,
        image,  # noqa: ANN001
        *,
        column: str,
        bbox: tuple[int, int, int, int],
    ) -> list[Anchor]:
        del bbox
        label = "100" if column == "L" else "101"
        return [Anchor(label=label, y_center=round(image.shape[0] * 140 / 842), confidence=99)]


class SyntheticEntryRecognizer:
    model_name = "synthetic-recognizer"

    def recognize(self, image, *, detector_label: str) -> EntryRecognition:  # noqa: ANN001
        del image
        if detector_label == "100":
            return EntryRecognition(elector_number="100", printed_marker=None)
        return EntryRecognition(elector_number="101", printed_marker="A")


def synthetic_two_column_register(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.draw_line((30, 100), (565, 100), width=1)
    page.insert_text((48, 140), "100")
    page.insert_text((120, 140), "SYNTHETIC ENTRY")
    page.draw_line((72, 136), (98, 136), width=2)
    page.insert_text((345, 140), "101")
    page.insert_text((390, 140), "A SYNTHETIC ENTRY")
    page.draw_line((30, 760), (565, 760), width=1)
    document.save(path)
    document.close()


def test_confirmed_pdf_becomes_neutral_reviewable_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    source_dir = workspace / "sources"
    source_dir.mkdir(parents=True)
    pdf_path = source_dir / "source.pdf"
    synthetic_two_column_register(pdf_path)
    job = Job(
        id="job-1",
        name="Synthetic review",
        workspace_path=workspace,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    source = SourceDocument(
        id="source-1",
        job_id=job.id,
        original_filename="HSLA.pdf",
        relative_path=Path("sources/source.pdf"),
        sha256="synthetic",
        page_count=1,
        polling_district="HSLA",
    )
    layout = LayoutPreview(
        id="layout-1",
        source_id=source.id,
        page_number=1,
        overlay_relative_path=Path("artifacts/layout/page_0001.png"),
        image_width=1240,
        image_height=1755,
        gutter_x=620,
        body_y0=210,
        body_y1=1584,
        review_status="confirmed",
    )
    engine = LocalRegisterEngine(
        anchor_detector=SyntheticAnchorDetector(),
        entry_recognizer=SyntheticEntryRecognizer(),
        dpi=300,
    )

    entries = engine.extract(job=job, source=source, layouts=[layout])

    assert [entry.elector_number for entry in entries] == ["100", "101"]
    assert [entry.column for entry in entries] == ["L", "R"]
    assert [entry.register_status for entry in entries] == [
        "voted_in_person",
        "no_in_person_mark",
    ]
    assert entries[1].printed_marker == "A"
    assert all((workspace / entry.crop_relative_path).is_file() for entry in entries)
    assert all(entry.review_outcome is None for entry in entries)
