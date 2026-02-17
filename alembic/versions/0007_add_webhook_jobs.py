"""create webhook_jobs table for async delivery queue

Revision ID: 0007_add_webhook_jobs
Revises: 0006_add_webhook_deliveries
Create Date: 2026-02-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_add_webhook_jobs"
down_revision = "0006_add_webhook_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("webhook_id", sa.Integer, sa.ForeignKey("policy_webhooks.id"), nullable=False),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_webhook_jobs_webhook_id", "webhook_jobs", ["webhook_id"])
    op.create_index("ix_webhook_jobs_tenant_id", "webhook_jobs", ["tenant_id"])
    op.create_index("ix_webhook_jobs_state", "webhook_jobs", ["state"])
    op.create_index("ix_webhook_jobs_scheduled_at", "webhook_jobs", ["scheduled_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_jobs_scheduled_at", table_name="webhook_jobs")
    op.drop_index("ix_webhook_jobs_state", table_name="webhook_jobs")
    op.drop_index("ix_webhook_jobs_tenant_id", table_name="webhook_jobs")
    op.drop_index("ix_webhook_jobs_webhook_id", table_name="webhook_jobs")
    op.drop_table("webhook_jobs")
