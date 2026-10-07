"""Conexión a Postgres (Supabase). El esquema lo crean las migraciones de supabase/migrations."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

if not settings.database_url.startswith("postgresql"):
    raise RuntimeError(
        "DATABASE_URL debe ser Postgres (Supabase): postgresql+asyncpg://... "
        "Ver backend/.env.example y docs/data-model.md"
    )

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Reportes: réplica de lectura opcional (DATABASE_URL_REPORTS). Sin réplica = la principal. Los refrescos de
# reporting.* (escrituras) siempre van a la principal; la réplica puede ir unos segundos atrás (§19.2).
reports_engine = (create_async_engine(settings.database_url_reports, pool_size=settings.db_pool_size,
                                      max_overflow=settings.db_max_overflow, pool_pre_ping=True)
                  if settings.database_url_reports else engine)
ReportsSessionLocal = async_sessionmaker(reports_engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def get_reports_session() -> AsyncIterator[AsyncSession]:
    async with ReportsSessionLocal() as session:
        yield session


async def set_actor(session: AsyncSession, actor_type: str, agent_id: int | None = None) -> None:
    """Indica a los triggers quién hace el cambio en la transacción actual (para conversation_events).

    actor_type: system | bot | agent | ai | contact | automation | flow
    Se debe llamar antes de escribir; vale hasta el próximo commit.
    """
    await session.execute(
        text("select set_config('app.actor_type', :t, true), set_config('app.actor_agent_id', :a, true)"),
        {"t": actor_type, "a": str(agent_id) if agent_id else ""},
    )


async def check_schema() -> None:
    """Verifica que las migraciones estén aplicadas (no crea nada)."""
    async with engine.connect() as conn:
        ok = await conn.scalar(text("select to_regclass('public.conversation_events') is not null"))
    if not ok:
        raise RuntimeError("El esquema no está creado: aplica las migraciones (npx supabase db push)")
