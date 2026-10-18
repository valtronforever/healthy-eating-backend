import os
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from fastmcp.utilities.asgi_transport import run_asgi_lifespan
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import healthy_eating.db.models  # noqa: F401  (registers all tables on Base.metadata)
from healthy_eating.config import Settings
from healthy_eating.db import Base
from healthy_eating.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://healthy:healthy@localhost:5432/healthy_eating_test",
)
BASE_URL = "http://localhost"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="development",
        base_url=BASE_URL,
        database_url=TEST_DATABASE_URL,
        dev_login_enabled=True,
    )


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    yield
    engine = create_async_engine(TEST_DATABASE_URL)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    await engine.dispose()


@pytest.fixture(scope="session")
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with run_asgi_lifespan(app):
        yield app


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client
