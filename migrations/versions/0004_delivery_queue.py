"""delivery queue index

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
    op.create_index(
        "ix_tickets_delivery_queue",
        "tickets",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status IN ('approved', 'sending')"),
    )


def downgrade() -> None:
    op.drop_index("ix_tickets_delivery_queue", table_name="tickets")
