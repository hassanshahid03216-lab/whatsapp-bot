import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Default environment variables for standalone test runs if not already configured in shell
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-characters-long")
os.environ.setdefault("DATABASE_URL", "postgresql://sellerbot_user:3cff3a25898a92b4e37b31e41b23dd94@localhost:5432/sellerbot")


@pytest.fixture(scope="session")
def engine():
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture()
def conn(engine):
    """One connection per test; everything is rolled back, nothing is ever committed."""
    connection = engine.connect()
    trans = connection.begin()
    try:
        yield connection
    finally:
        trans.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    from app.ratelimit import reset_all
    reset_all()
    yield
    reset_all()


@pytest.fixture(autouse=True)
def _fake_embeddings():
    from app.embeddings import FakeEmbeddingProvider, set_embedding_provider
    set_embedding_provider(FakeEmbeddingProvider())
    yield
    set_embedding_provider(None)


@pytest.fixture()
def db_session(conn):
    """App commits become savepoints, so the outer rollback still wipes everything."""
    session = Session(
        bind=conn, join_transaction_mode="create_savepoint", autoflush=False, expire_on_commit=False
    )
    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.main import app

    def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, base_url="https://testserver") as c:  # https so Secure cookies are sent
        yield c
    app.dependency_overrides.clear()
