"""
Pytest fixtures for Agent Trust Bureau tests.
"""
import pytest
import os

# Set test environment BEFORE any imports
os.environ["ATB_API_KEYS"] = "test-key-valid,another-valid-key"
os.environ["DATABASE_URL"] = "sqlite:///./test_trust_bureau.db"

# Remove any existing test db
if os.path.exists("./test_trust_bureau.db"):
    os.remove("./test_trust_bureau.db")

from fastapi.testclient import TestClient
from src.main import app
from src.database import Base, engine, init_db


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Create test database once for all tests."""
    # Create all tables
    Base.metadata.create_all(bind=engine)
    yield
    # Cleanup after all tests
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_trust_bureau.db"):
        os.remove("./test_trust_bureau.db")


@pytest.fixture(scope="function")
def client():
    """Test client for each test."""
    return TestClient(app)


@pytest.fixture
def valid_api_key():
    """Return a valid API key for testing."""
    return "test-key-valid"


@pytest.fixture
def invalid_api_key():
    """Return an invalid API key for testing."""
    return "invalid-key-12345"
