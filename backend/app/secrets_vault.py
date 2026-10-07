"""Secretos en Supabase Vault (claves de LLM, tokens de WhatsApp, firmas de webhooks).

Las tablas solo guardan el uuid del secreto. El backend (rol de servicio) los lee de vault.decrypted_secrets.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def put_secret(session: AsyncSession, value: str, name: str, secret_id: str | None = None) -> str:
    """Crea o actualiza un secreto y devuelve su id. `name` debe ser único (p. ej. 'ai_connection:12')."""
    if secret_id:
        await session.execute(text("select vault.update_secret(cast(:id as uuid), :v)"), {"id": secret_id, "v": value})
        return secret_id
    existing = await session.scalar(text("select id::text from vault.secrets where name = :n"), {"n": name})
    if existing:
        await session.execute(text("select vault.update_secret(cast(:id as uuid), :v)"), {"id": existing, "v": value})
        return existing
    return str(await session.scalar(text("select vault.create_secret(:v, :n)"), {"v": value, "n": name}))


async def get_secret(session: AsyncSession, secret_id: str | None) -> str | None:
    if not secret_id:
        return None
    return await session.scalar(
        text("select decrypted_secret from vault.decrypted_secrets where id = cast(:id as uuid)"), {"id": secret_id})


async def delete_secret(session: AsyncSession, secret_id: str | None) -> None:
    if secret_id:
        await session.execute(text("delete from vault.secrets where id = cast(:id as uuid)"), {"id": secret_id})
