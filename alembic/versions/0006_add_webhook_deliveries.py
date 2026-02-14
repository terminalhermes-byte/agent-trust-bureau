"""create webhook_deliveries table

Revision ID: 0006_add_webhook_deliveries
Revises: 0005_add_webhook_revoked_at
Create Date: 2026-02-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_add_webhook_deliveries"
down_revision = "0005_add_webhook_revoked_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("webhook_id", sa.Integer, sa.ForeignKey("policy_webhooks.id"), nullable=False, index=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False, default=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
