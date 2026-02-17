"""CLI utilities for bootstrapping tenants and API keys.

Usage:
    python -m app.cli bootstrap                    # create default tenant + key
    python -m app.cli create-key --tenant default  # add key to existing tenant
    python -m app.cli cleanup --days 30            # delete old deliveries + terminal jobs
    python -m app.cli cleanup --days 30 --dry-run  # preview what would be deleted
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.admin_store import cleanup_old_records
from app.auth import generate_api_key, hash_api_key
from app.db import get_engine, get_session_factory
from app.models import ApiKey, Base, Tenant


def _get_session():
    """Create a session for CLI operations."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    factory = get_session_factory()
    return factory()


def bootstrap() -> None:
    """Create default tenant and generate one API key. Idempotent for the tenant."""
    db = _get_session()
    try:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == "default")).first()
        if tenant is None:
            tenant = Tenant(name="Default", slug="default", is_active=True)
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
            print(f"Created tenant: {tenant.name} (id={tenant.id})")
        else:
            print(f"Tenant already exists: {tenant.name} (id={tenant.id})")

        raw_key, prefix = generate_api_key()
        key_row = ApiKey(
            tenant_id=tenant.id,
            key_prefix=prefix,
            key_hash=hash_api_key(raw_key),
            name="bootstrap-key",
        )
        db.add(key_row)
        db.commit()

        print()
        print("=" * 60)
        print("  API KEY (save this — it will NOT be shown again)")
        print(f"  {raw_key}")
        print("=" * 60)
        print()
        print(f"  Prefix:    {prefix}")
        print(f"  Tenant:    {tenant.slug} (id={tenant.id})")
        print(f"  Key name:  {key_row.name}")
        print()
    finally:
        db.close()


def create_key(tenant_slug: str, name: str = "default") -> None:
    """Generate a new API key for an existing tenant."""
    db = _get_session()
    try:
        tenant = db.scalars(select(Tenant).where(Tenant.slug == tenant_slug)).first()
        if tenant is None:
            print(f"Error: tenant '{tenant_slug}' not found. Run bootstrap first.", file=sys.stderr)
            sys.exit(1)

        raw_key, prefix = generate_api_key()
        key_row = ApiKey(
            tenant_id=tenant.id,
            key_prefix=prefix,
            key_hash=hash_api_key(raw_key),
            name=name,
        )
        db.add(key_row)
        db.commit()

        print()
        print("=" * 60)
        print("  API KEY (save this — it will NOT be shown again)")
        print(f"  {raw_key}")
        print("=" * 60)
        print()
        print(f"  Prefix:    {prefix}")
        print(f"  Tenant:    {tenant.slug} (id={tenant.id})")
        print(f"  Key name:  {key_row.name}")
        print()
    finally:
        db.close()


def cleanup(days: int, dry_run: bool = False) -> None:
    """Delete old webhook deliveries and terminal jobs older than *days* days."""
    db = _get_session()
    try:
        before = datetime.now(timezone.utc) - timedelta(days=days)
        deliveries_deleted, jobs_deleted = cleanup_old_records(
            db, before=before, dry_run=dry_run,
        )

        mode = "DRY RUN" if dry_run else "CLEANUP"
        print(f"\n  [{mode}] Records older than {days} days (before {before.date()}):")
        print(f"  Deliveries {'to delete' if dry_run else 'deleted'}: {deliveries_deleted}")
        print(f"  Jobs {'to delete' if dry_run else 'deleted'}:       {jobs_deleted}")
        print()
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="atb-cli", description="Agent Trust Bureau CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("bootstrap", help="Create default tenant and generate one API key")

    ck = sub.add_parser("create-key", help="Generate API key for a tenant")
    ck.add_argument("--tenant", required=True, help="Tenant slug")
    ck.add_argument("--name", default="default", help="Key name/label")

    cu = sub.add_parser("cleanup", help="Delete old deliveries and terminal jobs")
    cu.add_argument("--days", type=int, required=True, help="Delete records older than N days")
    cu.add_argument("--dry-run", action="store_true", help="Preview only, do not delete")

    args = parser.parse_args()

    if args.command == "bootstrap":
        bootstrap()
    elif args.command == "create-key":
        create_key(args.tenant, args.name)
    elif args.command == "cleanup":
        cleanup(args.days, args.dry_run)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
