"""Keyboard-first human review workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from marked_register.domain import Job, RegisterEntry, RegisterStatus, SourceDocument

_ELECTOR_RE = re.compile(r"^\d{1,5}(?:/\d{1,2})?$")
_STATUS_LABELS: dict[RegisterStatus, str] = {
    "voted_in_person": "Voted in person",
    "no_in_person_mark": "No in-person vote mark found",
    "struck_through": "Struck through",
    "unresolved": "Needs review",
}
_WARNING_LABELS = {
    "elector_number_missing": "Elector number could not be read",
    "detector_label_mismatch": "Number reader disagreed with the row detector",
    "possible_missing_row_before": "Possible missed row immediately above — check the full page",
}


class ReviewRepository(Protocol):
    """Metadata needed by the reviewer workflow."""

    def get(self, job_id: str) -> Job | None: ...

    def get_source(self, *, job_id: str, source_id: str) -> SourceDocument | None: ...

    def register_entries_for_job(self, job_id: str) -> list[RegisterEntry]: ...

    def get_register_entry(self, *, job_id: str, entry_id: str) -> RegisterEntry | None: ...

    def save_entry_review(
        self,
        *,
        job_id: str,
        entry_id: str,
        review_outcome: str | None,
        elector_number: str | None,
        register_status: str | None,
        note: str,
    ) -> bool: ...

    def insert_register_entry_after(
        self,
        *,
        job_id: str,
        after_entry_id: str,
        entry: RegisterEntry,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReviewQueue:
    """Current review position and progress."""

    entries: tuple[RegisterEntry, ...]
    current: RegisterEntry | None
    previous: RegisterEntry | None
    reviewed: int

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def unresolved(self) -> int:
        return sum(entry.review_outcome == "unresolved" for entry in self.entries)

    @property
    def rejected(self) -> int:
        return sum(entry.review_outcome == "rejected" for entry in self.entries)

    @property
    def last(self) -> RegisterEntry | None:
        return self.entries[-1] if self.entries else None


class Reviews:
    """Apply explicit human decisions to neutral predictions."""

    def __init__(self, repository: ReviewRepository) -> None:
        self._repository = repository

    def queue(self, *, job_id: str, entry_id: str | None = None) -> ReviewQueue:
        if self._repository.get(job_id) is None:
            raise LookupError("job not found")
        entries = tuple(self._repository.register_entries_for_job(job_id))
        current = None
        if entry_id is not None:
            current = next((entry for entry in entries if entry.id == entry_id), None)
            if current is None:
                raise LookupError("register entry not found")
        else:
            current = next((entry for entry in entries if entry.review_outcome is None), None)
        previous = None
        if current is not None:
            current_index = entries.index(current)
            if current_index > 0:
                previous = entries[current_index - 1]
        return ReviewQueue(
            entries=entries,
            current=current,
            previous=previous,
            reviewed=sum(entry.review_outcome is not None for entry in entries),
        )

    def apply(
        self,
        *,
        job_id: str,
        entry_id: str,
        action: str,
        elector_number: str,
        note: str,
    ) -> None:
        entry = self._repository.get_register_entry(job_id=job_id, entry_id=entry_id)
        if entry is None:
            raise LookupError("register entry not found")
        resolved_number = elector_number.strip() or None
        if resolved_number is not None and not _ELECTOR_RE.fullmatch(resolved_number):
            raise ValueError("elector number has an invalid format")

        review_outcome: str | None
        register_status: RegisterStatus | None
        if action == "undo":
            resolved_number = None
            register_status = None
            review_outcome = None
            note = ""
        elif action == "accept":
            if entry.elector_number is None:
                raise ValueError("correct or mark an unreadable elector number unresolved")
            resolved_number = entry.elector_number
            register_status = entry.register_status
            review_outcome = "confirmed"
        elif action in {"voted_in_person", "no_in_person_mark", "struck_through"}:
            if resolved_number is None:
                raise ValueError("elector number is required for a resolved entry")
            register_status = action  # type: ignore[assignment]
            review_outcome = (
                "confirmed"
                if resolved_number == entry.elector_number
                and register_status == entry.register_status
                else "corrected"
            )
        elif action == "unresolved":
            register_status = "unresolved"
            review_outcome = "unresolved"
        elif action == "reject":
            register_status = None
            review_outcome = "rejected"
        else:
            raise ValueError("unknown review action")

        if not self._repository.save_entry_review(
            job_id=job_id,
            entry_id=entry_id,
            review_outcome=review_outcome,
            elector_number=resolved_number,
            register_status=register_status,
            note=note.strip(),
        ):
            raise LookupError("register entry not found")

    def crop_path(self, *, job_id: str, entry_id: str) -> Path:
        job = self._repository.get(job_id)
        entry = self._repository.get_register_entry(job_id=job_id, entry_id=entry_id)
        if job is None or entry is None:
            raise LookupError("register entry not found")
        if entry.crop_relative_path is None:
            raise LookupError("manual entry has no row crop")
        path = (job.workspace_path / entry.crop_relative_path).resolve()
        if not path.is_relative_to(job.workspace_path.resolve()):
            raise ValueError("entry crop is outside the job workspace")
        return path

    def add_manual_entry(
        self,
        *,
        job_id: str,
        after_entry_id: str,
        elector_number: str,
        register_status: str,
        note: str,
    ) -> RegisterEntry:
        after = self._repository.get_register_entry(
            job_id=job_id,
            entry_id=after_entry_id,
        )
        if after is None:
            raise LookupError("register entry not found")
        resolved_number = elector_number.strip()
        if not _ELECTOR_RE.fullmatch(resolved_number):
            raise ValueError("elector number has an invalid format")
        if register_status not in _STATUS_LABELS or register_status == "unresolved":
            raise ValueError("choose a resolved register status for the missed elector")
        entry = RegisterEntry(
            id=f"{after.source_id}-manual-{uuid4()}",
            source_id=after.source_id,
            page_number=after.page_number,
            column=after.column,
            entry_index=after.entry_index + 1,
            crop_relative_path=None,
            detector_label="",
            elector_number=resolved_number,
            printed_marker=None,
            register_status=register_status,  # type: ignore[arg-type]
            prediction_reason="manually_added_from_page_context",
            recognizer_model="human",
            origin="manual",
            review_outcome="corrected",
            reviewed_elector_number=resolved_number,
            reviewed_register_status=register_status,  # type: ignore[arg-type]
            review_note=note.strip(),
        )
        if not self._repository.insert_register_entry_after(
            job_id=job_id,
            after_entry_id=after_entry_id,
            entry=entry,
        ):
            raise LookupError("register entry not found")
        return entry


def effective_elector_number(entry: RegisterEntry) -> str | None:
    return entry.reviewed_elector_number or entry.elector_number


def effective_register_status(entry: RegisterEntry) -> RegisterStatus:
    return entry.reviewed_register_status or entry.register_status


def status_label(status: RegisterStatus) -> str:
    """Return the stable user-facing label for a register status."""
    return _STATUS_LABELS[status]


def warning_label(warning: str) -> str:
    """Translate an internal warning into concise reviewer guidance."""
    return _WARNING_LABELS.get(warning, warning.replace("_", " ").capitalize())
