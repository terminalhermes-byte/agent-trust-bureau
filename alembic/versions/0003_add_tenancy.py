"""add tenants, api_keys tables and tenant_id to events + score_history

Revision ID: 0003_add_tenancy
Revises: 0002_create_score_history
Create Date: 2026-02-13 22:00:00.000000

Migration notes:
- Creates tenants and api_keys tables.
- Adds tenant_id (FK) to events and score_history.
- Inserts a default tenant (id=1, slug='default') and backfills existing rows.
- Replaces the global unique constraint on events.event_id with a
  per-tenant unique constraint on (tenant_id, event_id).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_add_tenancy"
down_revision = "0002_create_score_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- tenants ---
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(op.f("ix_tenants_slug"), "tenants", ["slug"], unique=True)

    # --- api_keys ---
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False, server_default=sa.text("'default'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_api_keys_key_prefix"), "api_keys", ["key_prefix"], unique=False)

    # --- insert default tenant for backfill ---
    tenants = sa.table("tenants", sa.column("id", sa.Integer), sa.column("name", sa.String), sa.column("slug", sa.String))
    op.bulk_insert(tenants, [{"id": 1, "name": "Default", "slug": "default"}])

    # --- add tenant_id to events ---
    op.add_column("events", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.execute("UPDATE events SET tenant_id = 1")
    op.alter_column("events", "tenant_id", nullable=False)
    op.create_foreign_key("fk_events_tenant_id", "events", "tenants", ["tenant_id"], ["id"])
    op.create_index(op.f("ix_events_tenant_id"), "events", ["tenant_id"], unique=False)

    # Replace global unique on event_id with per-tenant unique
    # 0001 created a UNIQUE constraint on events.event_id without an explicit name.
    # On Postgres this becomes something like `events_event_id_key`, so we must
    # locate and drop the constraint dynamically.
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for uc in inspector.get_unique_constraints("events"):
        if uc.get("column_names") == ["event_id"]:
            op.drop_constraint(uc["name"], "events", type_="unique")
            break
    op.create_unique_constraint("uq_events_tenant_event_id", "events", ["tenant_id", "event_id"])

    # --- add tenant_id to score_history ---
    op.add_column("score_history", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.execute("UPDATE score_history SET tenant_id = 1")
    op.alter_column("score_history", "tenant_id", nullable=False)
    op.create_foreign_key("fk_score_history_tenant_id", "score_history", "tenants", ["tenant_id"], ["id"])
    op.create_index(op.f("ix_score_history_tenant_id"), "score_history", ["tenant_id"], unique=False)


def downgrade() -> None:
    # score_history
    op.drop_index(op.f("ix_score_history_tenant_id"), table_name="score_history")
    op.drop_constraint("fk_score_history_tenant_id", "score_history", type_="foreignkey")
    op.drop_column("score_history", "tenant_id")

    # events — restore global unique on event_id
    op.drop_constraint("uq_events_tenant_event_id", "events", type_="unique")
    op.create_unique_constraint("uq_events_event_id", "events", ["event_id"])
    op.drop_index(op.f("ix_events_tenant_id"), table_name="events")
    op.drop_constraint("fk_events_tenant_id", "events", type_="foreignkey")
    op.drop_column("events", "tenant_id")

    # api_keys
    op.drop_index(op.f("ix_api_keys_key_prefix"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_tenant_id"), table_name="api_keys")
    op.drop_table("api_keys")

    # tenants
    op.drop_index(op.f("ix_tenants_slug"), table_name="tenants")
    op.drop_table("tenants")
