"""SQLAlchemy metadata adapter."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, Engine, ForeignKey, Integer, String, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from marked_register.domain import Job, LayoutPreview, RegisterEntry, SourceDocument


class Base(DeclarativeBase):
    """Application database metadata."""


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    workspace_path: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceDocumentRow(Base):
    __tablename__ = "source_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String)
    relative_path: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int] = mapped_column(Integer)
    polling_district: Mapped[str | None] = mapped_column(String, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(20), default="not_started")
    processing_error: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_pages: Mapped[int] = mapped_column(Integer, default=0)


class LayoutPreviewRow(Base):
    __tablename__ = "layout_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    overlay_relative_path: Mapped[str] = mapped_column(String)
    image_width: Mapped[int] = mapped_column(Integer)
    image_height: Mapped[int] = mapped_column(Integer)
    gutter_x: Mapped[int] = mapped_column(Integer)
    body_y0: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body_y1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    diagnostic: Mapped[str | None] = mapped_column(String, nullable=True)


class RegisterEntryRow(Base):
    __tablename__ = "register_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    column: Mapped[str] = mapped_column(String(1))
    entry_index: Mapped[int] = mapped_column(Integer)
    crop_relative_path: Mapped[str | None] = mapped_column(String, nullable=True)
    detector_label: Mapped[str] = mapped_column(String)
    elector_number: Mapped[str | None] = mapped_column(String, nullable=True)
    printed_marker: Mapped[str | None] = mapped_column(String, nullable=True)
    register_status: Mapped[str] = mapped_column(String(30))
    prediction_reason: Mapped[str] = mapped_column(String)
    recognizer_model: Mapped[str] = mapped_column(String)
    warnings_json: Mapped[str] = mapped_column(String, default="[]")
    origin: Mapped[str] = mapped_column(String(20), default="predicted")
    review_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reviewed_elector_number: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_register_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    review_note: Mapped[str] = mapped_column(String, default="")


def application_engine(database_url: str) -> Engine:
    """Build the configured metadata database engine."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def upgrade_database(database_url: str) -> None:
    """Bring the application metadata database to the current schema."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


class SqlAlchemyJobRepository:
    """SQLAlchemy adapter for persistent review jobs."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def add(self, job: Job) -> None:
        with Session(self._engine) as session:
            session.add(
                JobRow(
                    id=job.id,
                    name=job.name,
                    workspace_path=str(job.workspace_path),
                    created_at=job.created_at,
                )
            )
            session.commit()

    def get(self, job_id: str) -> Job | None:
        with Session(self._engine) as session:
            row = session.get(JobRow, job_id)
            if row is None:
                return None
            return Job(
                id=row.id,
                name=row.name,
                workspace_path=Path(row.workspace_path),
                created_at=row.created_at,
            )

    def recent(self, *, limit: int) -> list[Job]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
            )
            return [
                Job(
                    id=row.id,
                    name=row.name,
                    workspace_path=Path(row.workspace_path),
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def add_source(self, source: SourceDocument) -> None:
        with Session(self._engine) as session:
            session.add(
                SourceDocumentRow(
                    id=source.id,
                    job_id=source.job_id,
                    original_filename=source.original_filename,
                    relative_path=str(source.relative_path),
                    sha256=source.sha256,
                    page_count=source.page_count,
                    polling_district=source.polling_district,
                    processing_status=source.processing_status,
                    processing_error=source.processing_error,
                    processed_pages=source.processed_pages,
                )
            )
            session.commit()

    def sources_for_job(self, job_id: str) -> list[SourceDocument]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(SourceDocumentRow)
                .where(SourceDocumentRow.job_id == job_id)
                .order_by(SourceDocumentRow.original_filename, SourceDocumentRow.id)
            )
            return [
                SourceDocument(
                    id=row.id,
                    job_id=row.job_id,
                    original_filename=row.original_filename,
                    relative_path=Path(row.relative_path),
                    sha256=row.sha256,
                    page_count=row.page_count,
                    polling_district=row.polling_district,
                    processing_status=row.processing_status,  # type: ignore[arg-type]
                    processing_error=row.processing_error,
                    processed_pages=row.processed_pages,
                )
                for row in rows
            ]

    def get_source(self, *, job_id: str, source_id: str) -> SourceDocument | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(SourceDocumentRow).where(
                    SourceDocumentRow.id == source_id,
                    SourceDocumentRow.job_id == job_id,
                )
            )
            return None if row is None else _source_document(row)

    def set_polling_district(
        self,
        *,
        job_id: str,
        source_id: str,
        polling_district: str,
    ) -> bool:
        with Session(self._engine) as session:
            result = session.execute(
                update(SourceDocumentRow)
                .where(
                    SourceDocumentRow.id == source_id,
                    SourceDocumentRow.job_id == job_id,
                )
                .values(polling_district=polling_district)
            )
            session.commit()
            return result.rowcount == 1

    def set_processing_state(
        self,
        *,
        job_id: str,
        source_id: str,
        processing_status: str,
        processing_error: str | None = None,
        processed_pages: int | None = None,
    ) -> bool:
        values: dict[str, object] = {
            "processing_status": processing_status,
            "processing_error": processing_error,
        }
        if processed_pages is not None:
            values["processed_pages"] = processed_pages
        with Session(self._engine) as session:
            result = session.execute(
                update(SourceDocumentRow)
                .where(
                    SourceDocumentRow.id == source_id,
                    SourceDocumentRow.job_id == job_id,
                )
                .values(**values)
            )
            session.commit()
            return result.rowcount == 1

    def recover_interrupted_processing(self) -> int:
        with Session(self._engine) as session:
            result = session.execute(
                update(SourceDocumentRow)
                .where(SourceDocumentRow.processing_status.in_(("queued", "running")))
                .values(
                    processing_status="failed",
                    processing_error="Processing was interrupted. Retry when ready.",
                )
            )
            session.commit()
            return result.rowcount

    def replace_layout_previews(
        self,
        *,
        source_id: str,
        previews: Sequence[LayoutPreview],
    ) -> None:
        with Session(self._engine) as session:
            existing = session.scalars(
                select(LayoutPreviewRow).where(LayoutPreviewRow.source_id == source_id)
            )
            for row in existing:
                session.delete(row)
            session.add_all(
                [
                    LayoutPreviewRow(
                        id=preview.id,
                        source_id=preview.source_id,
                        page_number=preview.page_number,
                        overlay_relative_path=str(preview.overlay_relative_path),
                        image_width=preview.image_width,
                        image_height=preview.image_height,
                        gutter_x=preview.gutter_x,
                        body_y0=preview.body_y0,
                        body_y1=preview.body_y1,
                        review_status=preview.review_status,
                        diagnostic=preview.diagnostic,
                    )
                    for preview in previews
                ]
            )
            session.commit()

    def layout_previews_for_source(self, source_id: str) -> list[LayoutPreview]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(LayoutPreviewRow)
                .where(LayoutPreviewRow.source_id == source_id)
                .order_by(LayoutPreviewRow.page_number)
            )
            return [_layout_preview(row) for row in rows]

    def get_layout_preview(
        self,
        *,
        job_id: str,
        preview_id: str,
    ) -> LayoutPreview | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(LayoutPreviewRow)
                .join(SourceDocumentRow, SourceDocumentRow.id == LayoutPreviewRow.source_id)
                .where(
                    LayoutPreviewRow.id == preview_id,
                    SourceDocumentRow.job_id == job_id,
                )
            )
            return None if row is None else _layout_preview(row)

    def set_layout_review_status(
        self,
        *,
        source_id: str,
        review_status: Literal["confirmed", "rejected"],
    ) -> int:
        with Session(self._engine) as session:
            result = session.execute(
                update(LayoutPreviewRow)
                .where(
                    LayoutPreviewRow.source_id == source_id,
                    LayoutPreviewRow.review_status != "excluded",
                )
                .values(review_status=review_status)
            )
            session.commit()
            return result.rowcount

    def set_layout_preview_review_status(
        self,
        *,
        job_id: str,
        preview_id: str,
        review_status: str,
    ) -> bool:
        source_ids = select(SourceDocumentRow.id).where(SourceDocumentRow.job_id == job_id)
        with Session(self._engine) as session:
            result = session.execute(
                update(LayoutPreviewRow)
                .where(
                    LayoutPreviewRow.id == preview_id,
                    LayoutPreviewRow.source_id.in_(source_ids),
                )
                .values(review_status=review_status)
            )
            session.commit()
            return result.rowcount == 1

    def replace_register_entries(
        self,
        *,
        source_id: str,
        entries: Sequence[RegisterEntry],
    ) -> None:
        with Session(self._engine) as session:
            existing = session.scalars(
                select(RegisterEntryRow).where(RegisterEntryRow.source_id == source_id)
            )
            for row in existing:
                session.delete(row)
            session.add_all([_register_entry_row(entry) for entry in entries])
            session.commit()

    def register_entries_for_source(self, source_id: str) -> list[RegisterEntry]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(RegisterEntryRow)
                .where(RegisterEntryRow.source_id == source_id)
                .order_by(
                    RegisterEntryRow.page_number,
                    RegisterEntryRow.column,
                    RegisterEntryRow.entry_index,
                )
            )
            return [_register_entry(row) for row in rows]

    def register_entries_for_job(self, job_id: str) -> list[RegisterEntry]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(RegisterEntryRow)
                .join(SourceDocumentRow, SourceDocumentRow.id == RegisterEntryRow.source_id)
                .where(SourceDocumentRow.job_id == job_id)
                .order_by(
                    SourceDocumentRow.original_filename,
                    RegisterEntryRow.page_number,
                    RegisterEntryRow.column,
                    RegisterEntryRow.entry_index,
                )
            )
            return [_register_entry(row) for row in rows]

    def get_register_entry(self, *, job_id: str, entry_id: str) -> RegisterEntry | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(RegisterEntryRow)
                .join(SourceDocumentRow, SourceDocumentRow.id == RegisterEntryRow.source_id)
                .where(
                    RegisterEntryRow.id == entry_id,
                    SourceDocumentRow.job_id == job_id,
                )
            )
            return None if row is None else _register_entry(row)

    def save_entry_review(
        self,
        *,
        job_id: str,
        entry_id: str,
        review_outcome: str | None,
        elector_number: str | None,
        register_status: str | None,
        note: str,
    ) -> bool:
        source_ids = select(SourceDocumentRow.id).where(SourceDocumentRow.job_id == job_id)
        with Session(self._engine) as session:
            result = session.execute(
                update(RegisterEntryRow)
                .where(
                    RegisterEntryRow.id == entry_id,
                    RegisterEntryRow.source_id.in_(source_ids),
                )
                .values(
                    review_outcome=review_outcome,
                    reviewed_elector_number=elector_number,
                    reviewed_register_status=register_status,
                    review_note=note,
                )
            )
            session.commit()
            return result.rowcount == 1

    def insert_register_entry_after(
        self,
        *,
        job_id: str,
        after_entry_id: str,
        entry: RegisterEntry,
    ) -> bool:
        with Session(self._engine) as session:
            after = session.scalar(
                select(RegisterEntryRow)
                .join(SourceDocumentRow, SourceDocumentRow.id == RegisterEntryRow.source_id)
                .where(
                    RegisterEntryRow.id == after_entry_id,
                    SourceDocumentRow.job_id == job_id,
                )
            )
            if after is None or entry.source_id != after.source_id:
                return False
            session.execute(
                update(RegisterEntryRow)
                .where(
                    RegisterEntryRow.source_id == after.source_id,
                    RegisterEntryRow.page_number == after.page_number,
                    RegisterEntryRow.column == after.column,
                    RegisterEntryRow.entry_index > after.entry_index,
                )
                .values(entry_index=RegisterEntryRow.entry_index + 1)
            )
            session.add(_register_entry_row(entry))
            session.commit()
            return True


def _source_document(row: SourceDocumentRow) -> SourceDocument:
    return SourceDocument(
        id=row.id,
        job_id=row.job_id,
        original_filename=row.original_filename,
        relative_path=Path(row.relative_path),
        sha256=row.sha256,
        page_count=row.page_count,
        polling_district=row.polling_district,
        processing_status=row.processing_status,  # type: ignore[arg-type]
        processing_error=row.processing_error,
        processed_pages=row.processed_pages,
    )


def _layout_preview(row: LayoutPreviewRow) -> LayoutPreview:
    return LayoutPreview(
        id=row.id,
        source_id=row.source_id,
        page_number=row.page_number,
        overlay_relative_path=Path(row.overlay_relative_path),
        image_width=row.image_width,
        image_height=row.image_height,
        gutter_x=row.gutter_x,
        body_y0=row.body_y0,
        body_y1=row.body_y1,
        review_status=row.review_status,  # type: ignore[arg-type]
        diagnostic=row.diagnostic,
    )


def _register_entry_row(entry: RegisterEntry) -> RegisterEntryRow:
    return RegisterEntryRow(
        id=entry.id,
        source_id=entry.source_id,
        page_number=entry.page_number,
        column=entry.column,
        entry_index=entry.entry_index,
        crop_relative_path=(
            str(entry.crop_relative_path) if entry.crop_relative_path is not None else None
        ),
        detector_label=entry.detector_label,
        elector_number=entry.elector_number,
        printed_marker=entry.printed_marker,
        register_status=entry.register_status,
        prediction_reason=entry.prediction_reason,
        recognizer_model=entry.recognizer_model,
        warnings_json=json.dumps(entry.warnings),
        origin=entry.origin,
        review_outcome=entry.review_outcome,
        reviewed_elector_number=entry.reviewed_elector_number,
        reviewed_register_status=entry.reviewed_register_status,
        review_note=entry.review_note,
    )


def _register_entry(row: RegisterEntryRow) -> RegisterEntry:
    return RegisterEntry(
        id=row.id,
        source_id=row.source_id,
        page_number=row.page_number,
        column=row.column,  # type: ignore[arg-type]
        entry_index=row.entry_index,
        crop_relative_path=(Path(row.crop_relative_path) if row.crop_relative_path else None),
        detector_label=row.detector_label,
        elector_number=row.elector_number,
        printed_marker=row.printed_marker,
        register_status=row.register_status,  # type: ignore[arg-type]
        prediction_reason=row.prediction_reason,
        recognizer_model=row.recognizer_model,
        warnings=tuple(json.loads(row.warnings_json)),
        origin=row.origin,  # type: ignore[arg-type]
        review_outcome=row.review_outcome,  # type: ignore[arg-type]
        reviewed_elector_number=row.reviewed_elector_number,
        reviewed_register_status=row.reviewed_register_status,  # type: ignore[arg-type]
        review_note=row.review_note,
    )
