"""Create layout previews.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "layout_previews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("overlay_relative_path", sa.String(), nullable=False),
        sa.Column("image_width", sa.Integer(), nullable=False),
        sa.Column("image_height", sa.Integer(), nullable=False),
        sa.Column("gutter_x", sa.Integer(), nullable=False),
        sa.Column("body_y0", sa.Integer(), nullable=True),
        sa.Column("body_y1", sa.Integer(), nullable=True),
        sa.Column("review_status", sa.String(length=20), nullable=False),
        sa.Column("diagnostic", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["source_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_layout_previews_source_id"),
        "layout_previews",
        ["source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_layout_previews_source_id"), table_name="layout_previews")
    op.drop_table("layout_previews")
