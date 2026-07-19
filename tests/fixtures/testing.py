"""Shared pytest fixtures: a real Postgres test database (separate from the
dev DB), schema built from app.models directly (not via Alembic -- models.py
is what the migration was generated from, so there's no drift risk here, and
it's one less moving part for tests), full-table truncate between tests, and
an HTTP client wired to the app with the DB session swapped for the test one.
"""

from __future__ import annotations

from typing import AsyncIterator

import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app import models  # noqa: F401 -- registers all tables on Base.metadata
from app.core.config import settings
from app.core.db import Base, get_session
from app.main import app

TEST_DB_NAME = "payments_test"


def _test_database_url() -> str:
    base_url = settings.database_url.rsplit("/", 1)[0]
    return f"{base_url}/{TEST_DB_NAME}"


async def _ensure_test_database_exists() -> None:
    # asyncpg wants a plain postgresql:// DSN and a connection to a database
    # that already exists (postgres, the default maintenance DB) -- you can't
    # CREATE DATABASE while connected to the database you're creating.
    admin_dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME)
        if not exists:
            await conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine() -> AsyncIterator[AsyncEngine]:
    await _ensure_test_database_exists()
    engine = create_async_engine(_test_database_url(), pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(test_engine: AsyncEngine):
    yield
    # Truncate rather than rely on rollback: application code under test
    # (e.g. POST /events) calls session.commit() itself, so a test can't
    # simply roll back an outer transaction to undo it.
    async with test_engine.begin() as conn:
        await conn.execute(text("TRUNCATE payment_events, transactions, merchants RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_session(test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    session_factory = async_sessionmaker(bind=test_engine, expire_on_commit=False, autoflush=False)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
