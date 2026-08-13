"""Add persistent processing state.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_documents") as batch_op:
        batch_op.add_column(
            sa.Column(
                "processing_status",
                sa.String(length=20),
                nullable=False,
                server_default="not_started",
            )
        )
        batch_op.add_column(sa.Column("processing_error", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "processed_pages",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("source_documents") as batch_op:
        batch_op.drop_column("processed_pages")
        batch_op.drop_column("processing_error")
        batch_op.drop_column("processing_status")
