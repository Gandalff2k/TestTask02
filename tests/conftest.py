import asyncio
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.main import create_app


def _test_database_url() -> str:
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


async def _recreate_database(url: str) -> None:
    target = make_url(url)
    admin = await asyncpg.connect(
        host=target.host,
        port=target.port or 5432,
        user=target.username,
        password=target.password,
        database="postgres",
    )
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{target.database}"')
    finally:
        await admin.close()


def alembic_config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    asyncio.run(_recreate_database(url))
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
async def app(database_url: str) -> AsyncIterator[FastAPI]:
    application = create_app(Settings(database_url=database_url))
    async with application.state.session_factory() as session:
        await session.execute(text("TRUNCATE tickets, articles"))
        await session.commit()
    yield application
    await application.state.engine.dispose()


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    return factory


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
