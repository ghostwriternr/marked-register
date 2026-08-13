"""Create register entries.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "register_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("column", sa.String(length=1), nullable=False),
        sa.Column("entry_index", sa.Integer(), nullable=False),
        sa.Column("crop_relative_path", sa.String(), nullable=True),
        sa.Column("detector_label", sa.String(), nullable=False),
        sa.Column("elector_number", sa.String(), nullable=True),
        sa.Column("printed_marker", sa.String(), nullable=True),
        sa.Column("register_status", sa.String(length=30), nullable=False),
        sa.Column("prediction_reason", sa.String(), nullable=False),
        sa.Column("recognizer_model", sa.String(), nullable=False),
        sa.Column("warnings_json", sa.String(), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("review_outcome", sa.String(length=20), nullable=True),
        sa.Column("reviewed_elector_number", sa.String(), nullable=True),
        sa.Column("reviewed_register_status", sa.String(length=30), nullable=True),
        sa.Column("review_note", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["source_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_register_entries_source_id"),
        "register_entries",
        ["source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_register_entries_source_id"), table_name="register_entries")
    op.drop_table("register_entries")
