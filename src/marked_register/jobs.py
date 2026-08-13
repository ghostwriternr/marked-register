"""Job creation and retrieval."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol
from uuid import uuid4

import pymupdf

from marked_register.domain import Job, SourceDocument


class JobRepository(Protocol):
    """Persistent job collection."""

    def add(self, job: Job) -> None: ...

    def get(self, job_id: str) -> Job | None: ...

    def add_source(self, source: SourceDocument) -> None: ...

    def sources_for_job(self, job_id: str) -> list[SourceDocument]: ...

    def get_source(self, *, job_id: str, source_id: str) -> SourceDocument | None: ...

    def set_polling_district(
        self,
        *,
        job_id: str,
        source_id: str,
        polling_district: str,
    ) -> bool: ...


class SourceFileStore(Protocol):
    """Binary source-document storage."""

    def store(self, *, job: Job, source_id: str, content: bytes) -> Path: ...


class LocalSourceFileStore:
    """Store source PDFs as files relative to their job workspace."""

    def store(self, *, job: Job, source_id: str, content: bytes) -> Path:
        relative_path = Path("sources") / f"{source_id}.pdf"
        destination = job.workspace_path / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=destination.parent, prefix=".upload-", delete=False) as file:
            temporary_path = Path(file.name)
            file.write(content)
        replace(temporary_path, destination)
        return relative_path


class Jobs:
    """Product interface for persistent review jobs."""

    def __init__(self, repository: JobRepository, source_files: SourceFileStore) -> None:
        self._repository = repository
        self._source_files = source_files

    def create(self, *, name: str, workspace_path: Path) -> Job:
        resolved_name = name.strip()
        if not resolved_name:
            raise ValueError("job name is required")

        resolved_workspace = workspace_path.expanduser().resolve()
        if resolved_workspace.exists() and not resolved_workspace.is_dir():
            raise ValueError("job workspace must be a directory")
        resolved_workspace.mkdir(parents=True, exist_ok=True)

        job = Job(
            id=str(uuid4()),
            name=resolved_name,
            workspace_path=resolved_workspace,
            created_at=datetime.now(UTC),
        )
        self._repository.add(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._repository.get(job_id)

    def add_source(self, *, job_id: str, filename: str, content: bytes) -> SourceDocument:
        job = self.get(job_id)
        if job is None:
            raise LookupError("job not found")
        if Path(filename).suffix.lower() != ".pdf":
            raise ValueError("source document must be a PDF")

        try:
            with pymupdf.open(stream=content, filetype="pdf") as document:
                page_count = len(document)
        except Exception as error:
            raise ValueError("source document is not a readable PDF") from error
        if page_count == 0:
            raise ValueError("source PDF has no pages")

        source_id = str(uuid4())
        relative_path = self._source_files.store(
            job=job,
            source_id=source_id,
            content=content,
        )
        source = SourceDocument(
            id=source_id,
            job_id=job.id,
            original_filename=Path(filename).name,
            relative_path=relative_path,
            sha256=sha256(content).hexdigest(),
            page_count=page_count,
        )
        self._repository.add_source(source)
        return source

    def sources_for_job(self, job_id: str) -> list[SourceDocument]:
        return self._repository.sources_for_job(job_id)

    def get_source(self, *, job_id: str, source_id: str) -> SourceDocument | None:
        return self._repository.get_source(job_id=job_id, source_id=source_id)

    def confirm_polling_district(
        self,
        *,
        job_id: str,
        source_id: str,
        polling_district: str,
    ) -> None:
        resolved_district = " ".join(polling_district.split()).upper()
        if not resolved_district:
            raise ValueError("polling district is required")
        if len(resolved_district) > 50:
            raise ValueError("polling district must be 50 characters or fewer")
        if not self._repository.set_polling_district(
            job_id=job_id,
            source_id=source_id,
            polling_district=resolved_district,
        ):
            raise LookupError("source document not found")
