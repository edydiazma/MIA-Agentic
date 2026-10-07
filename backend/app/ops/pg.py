"""Conexiones asyncpg dedicadas (LISTEN, advisory locks): no pasan por el pool de SQLAlchemy."""

import asyncpg

from app.config import get_settings


def raw_dsn() -> str:
    """DATABASE_URL sin el driver de SQLAlchemy (postgresql+asyncpg:// → postgresql://)."""
    url = get_settings().database_url
    return "postgresql://" + url.split("://", 1)[1]


async def connect(**kw) -> asyncpg.Connection:
    # statement_cache_size=0: compatible con poolers (Supabase Session pooler / pgbouncer)
    return await asyncpg.connect(raw_dsn(), statement_cache_size=0, **kw)
