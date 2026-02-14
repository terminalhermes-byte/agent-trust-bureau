"""add policy_configs, agent_policy_overrides, policy_webhooks tables

Revision ID: 0004_add_policy_layer
Revises: 0003_add_tenancy
Create Date: 2026-02-13 23:00:00.000000

Migration notes:
- Creates policy_configs with sensible defaults (allow>=80, review>=60, block<40).
- Creates agent_policy_overrides for per-agent threshold tuning.
- Creates policy_webhooks for optional outbound notifications.
- Inserts a default policy_config for every existing tenant.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_add_policy_layer"
down_revision = "0003_add_tenancy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- policy_configs ---
    op.create_table(
        "policy_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("allow_threshold", sa.Float(), nullable=False, server_default=sa.text("80.0")),
        sa.Column("review_threshold", sa.Float(), nullable=False, server_default=sa.text("60.0")),
        sa.Column("block_threshold", sa.Float(), nullable=False, server_default=sa.text("40.0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_policy_configs_tenant"),
    )
    op.create_index(op.f("ix_policy_configs_tenant_id"), "policy_configs", ["tenant_id"], unique=False)

    # Seed default policy config for all existing tenants
    conn = op.get_bind()
    tenants = conn.execute(sa.text("SELECT id FROM tenants")).fetchall()
    for (tid,) in tenants:
        conn.execute(
            sa.text(
                "INSERT INTO policy_configs (tenant_id, allow_threshold, review_threshold, block_threshold) "
                "VALUES (:tid, 80.0, 60.0, 40.0)"
            ),
            {"tid": tid},
        )

    # --- agent_policy_overrides ---
    op.create_table(
        "agent_policy_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("allow_threshold", sa.Float(), nullable=True),
        sa.Column("review_threshold", sa.Float(), nullable=True),
        sa.Column("block_threshold", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "agent_id", name="uq_agent_policy_tenant_agent"),
    )
    op.create_index(op.f("ix_agent_policy_overrides_tenant_id"), "agent_policy_overrides", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_agent_policy_overrides_agent_id"), "agent_policy_overrides", ["agent_id"], unique=False)

    # --- policy_webhooks ---
    op.create_table(
        "policy_webhooks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(length=256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_policy_webhooks_tenant"),
    )
    op.create_index(op.f("ix_policy_webhooks_tenant_id"), "policy_webhooks", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_policy_webhooks_tenant_id"), table_name="policy_webhooks")
    op.drop_table("policy_webhooks")

    op.drop_index(op.f("ix_agent_policy_overrides_agent_id"), table_name="agent_policy_overrides")
    op.drop_index(op.f("ix_agent_policy_overrides_tenant_id"), table_name="agent_policy_overrides")
    op.drop_table("agent_policy_overrides")

    op.drop_index(op.f("ix_policy_configs_tenant_id"), table_name="policy_configs")
    op.drop_table("policy_configs")
