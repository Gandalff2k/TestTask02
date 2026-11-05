"""generation queue index and llm rate limit

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
    op.create_index(
        "ix_tickets_generation_queue",
        "tickets",
        ["next_attempt_at", "id"],
        postgresql_where=sa.text("status IN ('received', 'generating')"),
    )
    op.create_table(
        "llm_rate_limit",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "next_slot", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_llm_rate_limit_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_rate_limit")),
    )
    op.execute("INSERT INTO llm_rate_limit (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("llm_rate_limit")
    op.drop_index("ix_tickets_generation_queue", table_name="tickets")
