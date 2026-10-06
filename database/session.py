"""Async SQLAlchemy engine + session factory."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config.settings import settings


class Base(DeclarativeBase):
    pass


def _build_engine():
    url = settings.database_url
    kwargs: dict[str, object] = {"echo": settings.db_echo, "future": True}

    # SQLite in-memory databases are connection-local by default. Tests and
    # local smoke checks need every async session to see the same schema/data,
    # so use one shared connection for the lifetime of the process.
    if url.startswith("sqlite+aiosqlite:///:memory:"):
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    elif not url.startswith("sqlite"):
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    return create_async_engine(url, **kwargs)


_engine = None
_async_session_local = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_async_session_local():
    global _async_session_local
    if _async_session_local is None:
        _async_session_local = async_sessionmaker(
            bind=get_engine(), class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    return _async_session_local


def reset_engine() -> None:
    global _engine, _async_session_local
    old = _engine
    _engine = None
    _async_session_local = None
    if old is not None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(old.dispose())
        else:
            asyncio.get_running_loop().create_task(old.dispose())


class LazySessionFactory:
    def __call__(self, **kwargs):
        return get_async_session_local()(**kwargs)

    def __getattr__(self, name):
        return getattr(get_async_session_local(), name)


class LazyEngine:
    def __getattr__(self, name):
        return getattr(get_engine(), name)


engine = LazyEngine()
AsyncSessionLocal = LazySessionFactory()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = get_async_session_local()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = get_async_session_local()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize schema for tests/dev; production must use Alembic."""
    if settings.is_production:
        # Production schema is exclusively owned by the deployment migration
        # step. Application startup must never mutate production schema.
        return
    from database import models  # noqa: F401
    from database import auth_models  # noqa: F401
    from database import service_models  # noqa: F401
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await get_engine().dispose()
