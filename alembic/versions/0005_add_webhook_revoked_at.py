"""add revoked_at to policy_webhooks

Revision ID: 0005_add_webhook_revoked_at
Revises: 0004_add_policy_layer
Create Date: 2026-02-14 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_add_webhook_revoked_at"
down_revision = "0004_add_policy_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("policy_webhooks", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("policy_webhooks", "revoked_at")

