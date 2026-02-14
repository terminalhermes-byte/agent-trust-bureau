"""CLI utilities for bootstrapping tenants and API keys.

Usage:
    python -m app.cli bootstrap                   # create default tenant + key
    python -m app.cli create-key --tenant default  # add key to existing tenant
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="atb-cli", description="Agent Trust Bureau CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("bootstrap", help="Create default tenant and generate one API key")

    ck = sub.add_parser("create-key", help="Generate API key for a tenant")
    ck.add_argument("--tenant", required=True, help="Tenant slug")
    ck.add_argument("--name", default="default", help="Key name/label")

    args = parser.parse_args()

    if args.command == "bootstrap":
        bootstrap()
    elif args.command == "create-key":
        create_key(args.tenant, args.name)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
