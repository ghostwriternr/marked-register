"""SQLAlchemy metadata adapter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, Engine, ForeignKey, Integer, String, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from marked_register.domain import Job, SourceDocument


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
                )
                for row in rows
            ]

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
