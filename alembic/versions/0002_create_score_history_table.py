"""create score_history table

Revision ID: 0002_create_score_history
Revises: 0001_create_events_table
Create Date: 2026-02-13 20:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0002_create_score_history"
down_revision = "0001_create_events_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "score_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("factors", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_score_history_agent_id"), "score_history", ["agent_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_score_history_agent_id"), table_name="score_history")
    op.drop_table("score_history")
