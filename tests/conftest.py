from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_api_key
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import ApiKey, Base, Tenant
from app.rate_limit import reset_rate_limits


def _make_engine_and_session():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    return engine, testing_session


def _seed_default_tenant(session_factory):
    """Insert the default tenant so tenant_id=1 FK is valid."""
    db = session_factory()
    try:
        t = Tenant(id=1, name="Default", slug="default", is_active=True)
        db.add(t)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client() -> TestClient:
    """Unauthenticated client (require_auth=False). Seeds default tenant for FK integrity."""
    engine, testing_session = _make_engine_and_session()
    _seed_default_tenant(testing_session)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    original_require_auth = settings.require_auth
    settings.require_auth = False

    app.dependency_overrides[get_db] = override_get_db
    reset_rate_limits()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        settings.require_auth = original_require_auth
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        reset_rate_limits()
