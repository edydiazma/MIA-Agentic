"""Configuración por organización (tabla org_settings) con valores por defecto.

Algunos campos son "virtuales": se leen y escriben en su tabla normalizada para que la API no cambie:
- company.name / company.timezone          -> organizations
- conversations.typifications              -> typifications (activas, por posición)
- classifier.tags                          -> tags con ai_description (catálogo de la IA)
- classifier.typification_criteria         -> typifications.criteria
- learning.success_typifications           -> typifications.is_success
"""

import copy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ConfigRevision, Organization, OrgSetting, Tag, Typification, utcnow

DEFAULTS: dict[str, dict] = {
    "company": {"name": "Mi empresa", "timezone": "America/Bogota", "website": "", "address": ""},
    "conversations": {
        "typifications": [],
        "require_typification": True,
        "auto_assign": True,  # al transferir, asigna al asesor disponible con menos carga
        "sla_minutes": 5,  # nivel de servicio: tiempo máximo de primera respuesta del asesor
    },
    "reports": {"sla_target_pct": 80},
    "classifier": {
        "enabled": False,
        "cortex_id": None,  # None = Cortex principal
        "instructions": (
            "Analiza conversaciones de WhatsApp entre clientes, un bot y asesores de la empresa. "
            "Clasifica con criterio conservador: si la evidencia no es clara, deja el campo vacío y "
            "baja la confianza. Extrae datos del cliente solo si él los dijo explícitamente."
        ),
        "tags": [],
        "typification_criteria": {},
        "route_on_handoff": True,  # elige el grupo al transferir (si el bot no lo indicó)
        "classify_on_close": True,  # tipifica al cerrar
        "every_n_messages": 5,  # re-analiza cada N mensajes del cliente (0 = nunca)
        # auto = aplica | suggest = sugiere al asesor | off = no hace nada
        "modes": {"tags": "auto", "group": "auto", "typification": "suggest", "fields": "fill_empty", "memory": "auto"},
        "min_confidence": 0.7,
        "max_messages": 60,
    },
    "learning": {
        "cortex_id": None,  # None = el del clasificador o el principal
        "success_typifications": [],
        "batch_size": 6,
        "max_conversations": 60,
    },
    "catalog": {
        "currency": "COP",
        "meta_catalog_id": "",
        "send_as_catalog_message": True,
        "feed_url": "",
        "feed_format": "csv",
        "feed_interval_hours": 0,
        "last_sync": None,
    },
    # Registro maestro del cliente (llaves de identificación, documentos, oportunidades por vehículo)
    "golden": {
        "extract_documents": True,  # lee con IA las imágenes y PDF que envía el cliente (cédula, tarjeta de propiedad…)
        "extract_conversations": True,  # extrae llaves de la conversación (en el análisis del clasificador o al cerrar)
        "min_confidence": 0.6,  # llaves con menos confianza quedan solo en la auditoría de la extracción
        "agents_see_sensitive": False,  # los asesores ven documentos y fechas de nacimiento enmascarados
        "auto_opportunities": True,  # crea oportunidades por vencimientos del vehículo (SOAT, revisión, servicio)
        "opportunity_days": 30,
        "followup_on_opportunity": True,
    },
    # Seguridad de acceso (app/security/policy.py). Solo se edita por /api/security/policy (permiso security.manage).
    "security": {
        "min_length": 10,
        "require_upper": True,
        "require_lower": True,
        "require_digit": True,
        "require_symbol": False,
        "expiry_days": 0,  # 0 = la contraseña no vence
        "history": 5,  # no se pueden repetir las últimas N
        "lockout_attempts": 5,
        "lockout_minutes": 15,
        "allowed_ips": [],  # CIDR; vacío = cualquier IP
        "mfa_required": "none",  # none | admins | all
        "trusted_device_days": 30,
        "session_timeout_minutes": 720,
        "breached_check": False,  # consulta k-anónima a Have I Been Pwned al cambiar la contraseña
    },
    # Enrutamiento y operación del contact center (§18.1)
    "routing": {
        "sticky_agent": True,  # reasignar al mismo asesor que atendió antes si está disponible
        "owner_on_first_assignment": True,  # el primer asesor asignado queda como dueño del cliente
        "assign_when_none_available": False,  # en horario, si nadie está disponible, asigna a un integrante no disponible
        "session_timeout_minutes": 15,  # sin latido del panel → sesión cerrada y estado desconectado
        "offline_grace_seconds": 120,  # tras cerrar la última pestaña, espera antes de marcar desconectado
    },
    # Copiloto de IA para asesores y asistente del supervisor (§19.3)
    "copilot": {
        "enabled": True,
        "suggestions": "auto",  # auto = al llegar un mensaje del cliente | on_demand = solo al pedirlas
        "max_suggestions": 3,
        "max_per_conversation_per_hour": 12,  # tope de generaciones automáticas por conversación
        "debounce_seconds": 6,  # espera a que el cliente termine de escribir (ráfagas de mensajes)
        "history_messages": 20,
        "next_action": True,
        "handoff_summary": True,
        "close_summary": True,
        "assistant": True,
        # Cortex por función (None = el del propósito «copilot» / «assistant» o el principal)
        "reply_cortex_id": None,
        "summary_cortex_id": None,
        "assistant_cortex_id": None,
    },
    "appointments": {
        "enabled": False,
        "title": "Cita",
        "duration_min": 30,
        "days": [0, 1, 2, 3, 4],  # 0 = lunes
        "start": "08:00",
        "end": "18:00",
        "capacity": 1,
        "max_days_ahead": 30,
    },
}
VIRTUAL = {
    "company": {"name", "timezone"},
    "conversations": {"typifications"},
    "classifier": {"tags", "typification_criteria"},
    "learning": {"success_typifications"},
}

# Tags que se crean la primera vez que se activa el clasificador
DEFAULT_AI_TAGS = [
    ("cotizacion", "El cliente pide precio, cotización o condiciones de compra."),
    ("financiacion", "Pregunta por crédito, cuotas o financiación."),
    ("reclamo", "Queja, inconformidad o problema con un producto o servicio."),
    ("urgente", "El cliente expresa urgencia o un problema que no puede esperar."),
]


def org_id() -> int:
    return get_settings().organization_id


async def _typifications(session: AsyncSession, org: int) -> list[Typification]:
    return list((await session.scalars(
        select(Typification).where(Typification.organization_id == org, Typification.is_active)
        .order_by(Typification.position, Typification.id))).all())


async def get_setting(session: AsyncSession, key: str, org: int | None = None) -> dict:
    org = org or org_id()
    value = copy.deepcopy(DEFAULTS.get(key, {}))
    row = await session.get(OrgSetting, (org, key))
    if row and isinstance(row.value, dict):
        stored = {k: v for k, v in row.value.items() if k not in VIRTUAL.get(key, set())}
        if key == "classifier" and isinstance(stored.get("modes"), dict):
            stored["modes"] = {**value["modes"], **stored["modes"]}
        value.update(stored)

    if key == "company":
        o = await session.get(Organization, org)
        value["name"], value["timezone"] = o.name, o.timezone
    elif key == "conversations":
        value["typifications"] = [t.name for t in await _typifications(session, org)]
    elif key == "classifier":
        tags = (await session.scalars(select(Tag).where(Tag.organization_id == org, Tag.ai_description.is_not(None))
                                      .order_by(Tag.name))).all()
        value["tags"] = [{"name": t.name, "description": t.ai_description} for t in tags]
        value["typification_criteria"] = {t.name: t.criteria for t in await _typifications(session, org) if t.criteria}
    elif key == "learning":
        value["success_typifications"] = [t.name for t in await _typifications(session, org) if t.is_success]
    return value


async def _sync_typifications(session: AsyncSession, org: int, names: list[str]) -> None:
    existing = {t.name: t for t in (await session.scalars(
        select(Typification).where(Typification.organization_id == org))).all()}
    wanted = [n.strip() for n in names if n and n.strip()]
    for pos, name in enumerate(wanted, start=1):
        t = existing.get(name)
        if t:
            t.is_active, t.position = True, pos
        else:
            session.add(Typification(organization_id=org, name=name, position=pos))
    for name, t in existing.items():
        if name not in wanted:
            t.is_active = False  # nunca se borra: hay conversaciones y reportes que la referencian


async def _sync_ai_tags(session: AsyncSession, org: int, tags: list[dict]) -> None:
    existing = {t.name: t for t in (await session.scalars(select(Tag).where(Tag.organization_id == org))).all()}
    wanted = {}
    for t in tags:
        name = (t.get("name") or "").strip().lower()
        if name:
            wanted[name] = (t.get("description") or "").strip() or name
    for name, desc in wanted.items():
        if name in existing:
            existing[name].ai_description = desc
        else:
            session.add(Tag(organization_id=org, name=name, ai_description=desc))
    for name, tag in existing.items():
        if name not in wanted:
            tag.ai_description = None  # sale del catálogo de la IA; las conversaciones conservan la etiqueta


async def set_setting(
    session: AsyncSession, key: str, value: dict, org: int | None = None, agent_id: int | None = None,
    source: str = "human",
) -> dict:
    if key not in DEFAULTS:
        raise KeyError(key)
    org = org or org_id()
    virtual = VIRTUAL.get(key, set())

    if key == "company":
        o = await session.get(Organization, org)
        if value.get("name"):
            o.name = value["name"]
        if value.get("timezone"):
            o.timezone = value["timezone"]
    elif key == "conversations" and "typifications" in value:
        await _sync_typifications(session, org, value["typifications"])
    elif key == "classifier":
        if "tags" in value:
            await _sync_ai_tags(session, org, value["tags"])
        if "typification_criteria" in value:
            for t in await _typifications(session, org):
                t.criteria = (value["typification_criteria"].get(t.name) or "").strip() or None
    elif key == "learning" and "success_typifications" in value:
        success = set(value["success_typifications"])
        for t in await _typifications(session, org):
            t.is_success = t.name in success

    row = await session.get(OrgSetting, (org, key))
    stored = dict(row.value) if row else {}
    stored.update({k: v for k, v in value.items() if k in DEFAULTS[key] and k not in virtual})
    if key == "classifier" and isinstance(value.get("modes"), dict):
        stored["modes"] = {**DEFAULTS["classifier"]["modes"], **(row.value.get("modes", {}) if row else {}),
                           **value["modes"]}
    if row:
        row.value, row.updated_by, row.updated_at = stored, agent_id, utcnow()
    else:
        session.add(OrgSetting(organization_id=org, key=key, value=stored, updated_by=agent_id))
    await session.flush()

    full = await get_setting(session, key, org)
    await add_revision(session, org, "setting", key, full, source, agent_id)
    await session.commit()
    return full


async def add_revision(
    session: AsyncSession, org: int, entity_type: str, entity_id: str | int, document: dict, source: str,
    agent_id: int | None = None, ai_prompt: str | None = None, ai_call_id: int | None = None,
) -> ConfigRevision:
    """Guarda una versión JSON de la entidad (historial editable por personas o IA)."""
    last = await session.scalar(
        select(ConfigRevision.revision).where(
            ConfigRevision.organization_id == org, ConfigRevision.entity_type == entity_type,
            ConfigRevision.entity_id == str(entity_id)).order_by(ConfigRevision.revision.desc()).limit(1))
    rev = ConfigRevision(organization_id=org, entity_type=entity_type, entity_id=str(entity_id),
                         revision=(last or 0) + 1, document=document, source=source, ai_prompt=ai_prompt,
                         ai_call_id=ai_call_id, created_by=agent_id)
    session.add(rev)
    return rev


async def ensure_default_ai_tags(session: AsyncSession, org: int | None = None) -> None:
    org = org or org_id()
    if not await session.scalar(select(Tag.id).where(Tag.organization_id == org, Tag.ai_description.is_not(None))):
        await _sync_ai_tags(session, org, [{"name": n, "description": d} for n, d in DEFAULT_AI_TAGS])
