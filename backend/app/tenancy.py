"""Multiempresa: alta de organizaciones, datos por defecto, estado y resolución del tenant desde webhooks.

Reglas:
- El tenant de una petición autenticada sale del token (claim `org`), nunca de la configuración.
- El tenant de un webhook de Meta sale de `channels.phone_number_id` (o `waba_id`); si no se conoce, es NULL.
"""

import math
import os
import re
import secrets
import unicodedata
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    AIAgent,
    AIConnection,
    Channel,
    Cortex,
    CortexMember,
    Organization,
    Plan,
    Subscription,
    Typification,
    utcnow,
)

settings = get_settings()

BLOCKED_STATUSES = ("suspended", "cancelled")
DEFAULT_TYPIFICATIONS = [("Venta", True), ("Consulta resuelta", False), ("Cotización enviada", False),
                         ("Reclamo", False), ("Sin respuesta", False), ("Spam", False), ("Inactividad", False)]
DEFAULT_PROMPT = """Eres el asistente virtual de la empresa. Atiendes a los clientes por WhatsApp en español, \
con un tono cercano y profesional.

## Objetivo
Resolver dudas frecuentes, recopilar los datos necesarios del cliente y transferir a un asesor humano cuando \
haga falta.

## Información del negocio
(Completa aquí: productos, precios, horarios, sedes, políticas, preguntas frecuentes.)
"""


def slugify(text: str) -> str:
    s = unicodedata.normalize("NFKD", text.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40] or "empresa"


def trial_days_left(org: Organization) -> int | None:
    if org.status != "trial" or not org.trial_ends_at:
        return None
    return max(0, math.ceil((org.trial_ends_at - utcnow()).total_seconds() / 86400))


def org_access_error(org: Organization | None) -> tuple[int, str] | None:
    """(código HTTP, mensaje) si la empresa no puede operar; None si puede."""
    if org is None:
        return 403, "La empresa no existe"
    if org.status == "suspended":
        return 403, "La empresa está suspendida. Contacta a soporte."
    if org.status == "cancelled":
        return 402, "La suscripción de la empresa está cancelada. Reactívala en Plan y facturación."
    if org.status == "trial" and org.trial_ends_at and org.trial_ends_at < utcnow():
        return 402, "El período de prueba terminó. Elige un plan en Plan y facturación para continuar."
    return None


async def plan_by_key(session: AsyncSession, key: str | None) -> Plan | None:
    if not key:
        return None
    return await session.scalar(select(Plan).where(Plan.key == key))


async def unique_slug(session: AsyncSession, base: str) -> str:
    slug = slugify(base)
    if not await session.scalar(select(Organization.id).where(Organization.slug == slug)):
        return slug
    return f"{slug}-{secrets.token_hex(3)}"


async def create_org(session: AsyncSession, name: str, country: str | None, plan_key: str | None,
                     status: str = "trial", billing_email: str | None = None) -> Organization:
    plan = await plan_by_key(session, plan_key) or await plan_by_key(session, settings.saas_default_plan)
    org = Organization(
        name=name.strip(), slug=await unique_slug(session, name), status=status,
        plan_id=plan.id if plan else None, country=(country or "").upper()[:2] or None, billing_email=billing_email,
        trial_ends_at=utcnow() + timedelta(days=settings.saas_trial_days) if status == "trial" else None,
    )
    session.add(org)
    await session.flush()
    if plan:
        session.add(Subscription(organization_id=org.id, plan_id=plan.id, provider="manual",
                                 status="trialing" if status == "trial" else "active",
                                 current_period_start=utcnow(), current_period_end=org.trial_ends_at))
    for pos, (tname, success) in enumerate(DEFAULT_TYPIFICATIONS, start=1):
        session.add(Typification(organization_id=org.id, name=tname, is_success=success, position=pos))
    await session.flush()
    await __import__("app.quality.defaults", fromlist=["x"]).ensure_defaults(session, org.id)  # rúbricas QA
    return org


async def provision_ai_defaults(session: AsyncSession, org: int) -> AIAgent:
    """Conexiones de IA del servidor (si hay claves), Cortex «Principal» y un agente de IA inicial."""
    cortex = await session.scalar(select(Cortex).where(Cortex.organization_id == org).order_by(Cortex.id).limit(1))
    if not await session.scalar(select(AIConnection.id).where(AIConnection.organization_id == org).limit(1)):
        conns = []
        if settings.anthropic_api_key:
            conns.append(AIConnection(
                organization_id=org, name="Claude (servidor)", provider="anthropic", model=settings.default_model,
                default_params={"effort": settings.default_effort} if settings.default_effort else {}))
        if settings.openai_api_key:
            conns.append(AIConnection(organization_id=org, name="OpenAI (servidor)", provider="openai",
                                      model=os.environ.get("OPENAI_MODEL", "gpt-4.1")))
        session.add_all(conns)
        await session.flush()
        if conns and not cortex:
            cortex = Cortex(organization_id=org, name="Principal", purpose="any", strategy="failover",
                            description="Conexiones del servidor; Claude primero y OpenAI como respaldo")
            session.add(cortex)
            await session.flush()
            for pos, c in enumerate(conns, start=1):
                session.add(CortexMember(cortex_id=cortex.id, connection_id=c.id, position=pos))
    agent = await session.scalar(select(AIAgent).where(AIAgent.organization_id == org).order_by(AIAgent.id).limit(1))
    if not agent:
        agent = AIAgent(organization_id=org, name="Asistente principal", system_prompt=DEFAULT_PROMPT,
                        cortex_id=cortex.id if cortex else None)
        session.add(agent)
        await session.flush()
    return agent


# --- Webhooks: resolver la empresa sin depender de la configuración -------------
def payload_phone_ids(payload: dict) -> tuple[set[str], set[str]]:
    """(phone_number_ids, waba_ids) presentes en un webhook de WhatsApp."""
    phones, wabas = set(), set()
    for entry in payload.get("entry") or []:
        if entry.get("id"):
            wabas.add(str(entry["id"]))
        for change in entry.get("changes") or []:
            pid = ((change.get("value") or {}).get("metadata") or {}).get("phone_number_id")
            if pid:
                phones.add(str(pid))
    return phones, wabas


async def org_for_webhook(session: AsyncSession, payload: dict) -> int | None:
    phones, wabas = payload_phone_ids(payload)
    if not phones and not wabas:
        return None
    conds = []
    if phones:
        conds.append(Channel.phone_number_id.in_(phones))
    if wabas:
        conds.append(Channel.waba_id.in_(wabas))
    orgs = set((await session.scalars(select(Channel.organization_id).where(or_(*conds)))).all())
    return orgs.pop() if len(orgs) == 1 else None


async def count_orgs(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Organization)) or 0
