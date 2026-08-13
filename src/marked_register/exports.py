"""Reviewed operational exports."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Protocol

from marked_register.domain import Job, RegisterEntry, SourceDocument
from marked_register.reviews import effective_elector_number, effective_register_status

CSV_COLUMNS = (
    "electoral_reference",
    "polling_district",
    "elector_number",
    "register_status",
    "printed_marker",
    "review_outcome",
    "review_note",
    "source_file",
    "page_number",
    "source_entry_id",
)


class ExportNotReadyError(RuntimeError):
    """The job does not yet satisfy the human-review gate."""


class ExportRepository(Protocol):
    """Metadata needed to gate and build reviewed exports."""

    def get(self, job_id: str) -> Job | None: ...

    def register_entries_for_job(self, job_id: str) -> list[RegisterEntry]: ...

    def sources_for_job(self, job_id: str) -> list[SourceDocument]: ...


@dataclass(frozen=True, slots=True)
class GeneratedExports:
    """Files created for one reviewed job."""

    all_electors: Path
    voted_only: Path
    manifest: Path


class Exports:
    """Create CSVs only from entries with explicit human decisions."""

    def __init__(self, repository: ExportRepository) -> None:
        self._repository = repository

    def ensure_ready(self, job_id: str) -> None:
        if self._repository.get(job_id) is None:
            raise LookupError("job not found")
        entries = self._repository.register_entries_for_job(job_id)
        remaining = sum(entry.review_outcome is None for entry in entries)
        if remaining:
            noun = "entry" if remaining == 1 else "entries"
            raise ExportNotReadyError(f"{remaining} {noun} still need review")
        if not entries:
            raise ExportNotReadyError("no reviewed entries are available to export")

    def generate(self, job_id: str) -> GeneratedExports:
        """Atomically replace a job's reviewed exports and manifest."""
        self.ensure_ready(job_id)
        job = self._repository.get(job_id)
        if job is None:  # Defensive: readiness already verifies this.
            raise LookupError("job not found")
        sources = self._repository.sources_for_job(job_id)
        entries = self._repository.register_entries_for_job(job_id)
        sources_by_id = {source.id: source for source in sources}
        exported_entries = [entry for entry in entries if entry.review_outcome != "rejected"]
        rows = [_csv_row(entry, sources_by_id[entry.source_id]) for entry in exported_entries]
        voted_rows = [row for row in rows if row["register_status"] == "voted_in_person"]

        export_dir = job.workspace_path / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        all_path = export_dir / "all-electors.csv"
        voted_path = export_dir / "voted-only.csv"
        manifest_path = export_dir / "manifest.json"
        _write_csv(all_path, rows)
        _write_csv(voted_path, voted_rows)
        manifest = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "job": {"id": job.id, "name": job.name},
            "sources": [
                {
                    "id": source.id,
                    "filename": source.original_filename,
                    "sha256": source.sha256,
                    "page_count": source.page_count,
                    "polling_district": source.polling_district,
                }
                for source in sources
            ],
            "recognizer_models": sorted(
                {
                    entry.recognizer_model
                    for entry in entries
                    if entry.origin == "predicted"
                }
            ),
            "review_summary": {
                "extracted_rows": len(entries),
                "exported_rows": len(exported_entries),
                "rejected_rows": sum(
                    entry.review_outcome == "rejected" for entry in entries
                ),
                "unresolved_rows": sum(
                    entry.review_outcome == "unresolved" for entry in entries
                ),
            },
            "exports": [
                {
                    "filename": all_path.name,
                    "row_count": len(rows),
                    "sha256": _sha256(all_path),
                },
                {
                    "filename": voted_path.name,
                    "row_count": len(voted_rows),
                    "sha256": _sha256(voted_path),
                },
            ],
        }
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        return GeneratedExports(all_path, voted_path, manifest_path)

    def available(self, job_id: str) -> bool:
        job = self._repository.get(job_id)
        if job is None:
            return False
        export_dir = job.workspace_path / "exports"
        return all(
            (export_dir / filename).is_file()
            for filename in ("all-electors.csv", "voted-only.csv", "manifest.json")
        )

    def download_path(
        self,
        *,
        job_id: str,
        export_name: Literal["all-electors", "voted-only", "manifest"],
    ) -> Path:
        job = self._repository.get(job_id)
        if job is None:
            raise LookupError("job not found")
        filenames = {
            "all-electors": "all-electors.csv",
            "voted-only": "voted-only.csv",
            "manifest": "manifest.json",
        }
        path = (job.workspace_path / "exports" / filenames[export_name]).resolve()
        if not path.is_relative_to(job.workspace_path.resolve()) or not path.is_file():
            raise LookupError("export not found")
        return path


def _csv_row(entry: RegisterEntry, source: SourceDocument) -> dict[str, str | int | None]:
    elector_number = effective_elector_number(entry)
    reference = (
        f"{source.polling_district}-{elector_number}"
        if source.polling_district and elector_number
        else ""
    )
    return {
        "electoral_reference": reference,
        "polling_district": source.polling_district or "",
        "elector_number": elector_number or "",
        "register_status": effective_register_status(entry),
        "printed_marker": entry.printed_marker or "",
        "review_outcome": entry.review_outcome,
        "review_note": entry.review_note,
        "source_file": source.original_filename,
        "page_number": entry.page_number,
        "source_entry_id": entry.id,
    }


def _write_csv(path: Path, rows: list[dict[str, str | int | None]]) -> None:
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as file:
        temporary_path = Path(file.name)
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary_path.replace(path)


def _atomic_write_text(path: Path, content: str) -> None:
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as file:
        temporary_path = Path(file.name)
        file.write(content)
    temporary_path.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
