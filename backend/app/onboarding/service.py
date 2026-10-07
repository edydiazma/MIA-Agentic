"""Estado del asistente (onboarding_runs / onboarding_steps) y pasos automáticos idempotentes."""

import copy
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.router import json_call
from app.config import get_settings
from app.models import (
    AgentInvitation,
    AIAgent,
    AIAgentKnowledge,
    Automation,
    Channel,
    Group,
    KnowledgeDoc,
    OnboardingRun,
    OnboardingStep,
    Organization,
    WaTemplate,
    utcnow,
)
from app.onboarding import checks, graph, meta, packs

log = logging.getLogger(__name__)

STEPS: list[tuple[str, str, str, bool]] = [
    ("company", "Tu empresa", "Datos, industria, horario y tono. Puedes importarlos desde tu sitio web.", True),
    ("whatsapp", "Conectar WhatsApp", "Inicia sesión con Meta y elige o crea tu número de WhatsApp Business.", True),
    ("validate", "Validar el número", "Revisamos registro, webhooks, calidad, límites y nombre; arreglamos lo posible.",
     True),
    ("profile", "Perfil de WhatsApp", "Descripción, dirección, correo y sitio web que ven tus clientes.", False),
    ("templates", "Plantillas", "Paquete de plantillas de tu industria enviado a aprobación de Meta.", True),
    ("ai", "Agente de IA", "Instrucciones del agente, conocimiento, tipificaciones, grupos y horario.", False),
    ("team", "Tu equipo", "Invita a tus asesores y supervisores.", False),
    ("test", "Prueba", "Enviamos un mensaje a tu teléfono y esperamos tu respuesta.", False),
    ("go_live", "Salir en vivo", "Activamos el bot y empiezas a atender.", True),
]
STEP_KEYS = [s[0] for s in STEPS]
REQUIRED = {s[0] for s in STEPS if s[3]}


class StepError(Exception):
    """Error de un paso con mensaje para el usuario; `blocking` lista lo que impide avanzar."""

    def __init__(self, message: str, status: int = 409, blocking: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.blocking = blocking or []


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base or {})
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def imported(answers: dict) -> dict:
    """Importación del sitio: el panel la guarda en `import`; el backend en `website_import`."""
    return (answers or {}).get("import") or (answers or {}).get("website_import") or {}


def hours_config(answers: dict) -> dict | None:
    """Horario → regla business_hours ({days, start, end}). Acepta company.hours por día
    ({mon: {open, from, to}, …}) o el formato plano answers.hours {days, start, end}."""
    flat = (answers or {}).get("hours") or {}
    if flat.get("start") and flat.get("end"):
        return {"days": flat.get("days") or [0, 1, 2, 3, 4], "start": flat["start"], "end": flat["end"]}
    per_day = ((answers or {}).get("company") or {}).get("hours") or {}
    open_days = [(i, per_day[k]) for i, k in enumerate(DAY_KEYS) if isinstance(per_day.get(k), dict)
                 and per_day[k].get("open") and per_day[k].get("from") and per_day[k].get("to")]
    if not open_days:
        return None
    # Una regla por organización: la franja más amplia de los días abiertos
    return {"days": [i for i, _d in open_days], "start": min(d["from"] for _i, d in open_days),
            "end": max(d["to"] for _i, d in open_days)}


def ai_toggles(answers: dict) -> dict:
    ai = (answers or {}).get("ai") or {}
    keys = ("apply_prompt", "apply_knowledge", "apply_catalog", "apply_typifications", "apply_groups",
            "apply_hours_message")
    return {k: ai.get(k, True) is not False for k in keys}


# --- Runs y pasos ------------------------------------------------------------------
async def active_run(session: AsyncSession, org: int, create: bool = True,
                     agent_id: int | None = None) -> OnboardingRun | None:
    run = (await session.scalars(select(OnboardingRun).where(
        OnboardingRun.organization_id == org, OnboardingRun.status == "in_progress").limit(1))).first()
    if run or not create:
        return run
    o = await session.get(Organization, org)
    if o.onboarding_completed_at is not None:
        return None
    return await start_run(session, org, agent_id)


async def start_run(session: AsyncSession, org: int, agent_id: int | None,
                    channel_id: int | None = None) -> OnboardingRun:
    o = await session.get(Organization, org)
    adopted = False
    if channel_id is None:  # la empresa ya tiene un número de WhatsApp: el asistente lo usa y valida
        channel_id = await session.scalar(select(Channel.id).where(
            Channel.organization_id == org, Channel.provider == "whatsapp_cloud").order_by(Channel.id.desc()).limit(1))
        adopted = channel_id is not None
    run = OnboardingRun(organization_id=org, started_by=agent_id, industry=o.industry, channel_id=channel_id,
                        answers={"company": {"name": o.name, "timezone": o.timezone, "country": o.country}})
    session.add(run)
    await session.flush()
    for key in STEP_KEYS:
        st = OnboardingStep(run_id=run.id, key=key)
        if key == "whatsapp" and adopted:
            st.status, st.result, st.finished_at = "done", {"adopted_channel_id": channel_id}, utcnow()
        session.add(st)
    await session.flush()
    return run


async def latest_run(session: AsyncSession, org: int) -> OnboardingRun | None:
    return (await session.scalars(select(OnboardingRun).where(OnboardingRun.organization_id == org)
                                  .order_by(OnboardingRun.id.desc()).limit(1))).first()


async def steps_of(session: AsyncSession, run: OnboardingRun) -> dict[str, OnboardingStep]:
    return {s.key: s for s in (await session.scalars(
        select(OnboardingStep).where(OnboardingStep.run_id == run.id))).all()}


async def step(session: AsyncSession, run: OnboardingRun, key: str) -> OnboardingStep:
    row = (await steps_of(session, run)).get(key)
    if row is None:
        row = OnboardingStep(run_id=run.id, key=key)
        session.add(row)
        await session.flush()
    return row


def begin(row: OnboardingStep) -> None:
    row.status, row.attempts, row.started_at, row.error = "running", (row.attempts or 0) + 1, utcnow(), None


def finish(row: OnboardingStep, status: str, result: dict | None = None, error: str | None = None) -> None:
    row.status, row.finished_at, row.error = status, utcnow(), error
    if result is not None:
        row.result = {**(row.result or {}), **result}


def advance(run: OnboardingRun, steps: dict[str, OnboardingStep]) -> None:
    """current_step = primer paso no resuelto."""
    for key in STEP_KEYS:
        s = steps.get(key)
        if not s or s.status not in ("done", "skipped", "warning"):
            run.current_step = key
            return
    run.current_step = "go_live"


def step_out(s: OnboardingStep) -> dict:
    meta_ = next(x for x in STEPS if x[0] == s.key)
    return {"key": s.key, "label": meta_[1], "description": meta_[2], "status": s.status, "attempts": s.attempts,
            "result": s.result or {}, "error": s.error, "required": meta_[3]}


def run_out(run: OnboardingRun | None) -> dict | None:
    if run is None:
        return None
    return {"id": run.id, "status": run.status, "current_step": run.current_step, "industry": run.industry,
            "answers": run.answers or {}, "channel_id": run.channel_id, "started_at": run.started_at,
            "completed_at": run.completed_at}


async def run_channel(session: AsyncSession, run: OnboardingRun) -> Channel:
    channel = await session.get(Channel, run.channel_id) if run.channel_id else None
    if channel is None or channel.organization_id != run.organization_id:
        channel = (await session.scalars(select(Channel).where(
            Channel.organization_id == run.organization_id, Channel.provider == "whatsapp_cloud")
            .order_by(Channel.id).limit(1))).first()
        if channel is not None:
            run.channel_id = channel.id
    if channel is None:
        raise StepError("Primero conecta tu número de WhatsApp", blocking=["whatsapp"])
    return channel


# --- Paso: empresa ---------------------------------------------------------------------
async def save_answers(session: AsyncSession, run: OnboardingRun, industry: str | None, answers: dict,
                       agent_id: int | None, current_step: str | None = None) -> OnboardingRun:
    from app.settings_store import set_setting

    if current_step is not None and current_step not in STEP_KEYS:
        raise StepError(f"Paso inválido: {current_step}", status=422)
    if industry is None and isinstance((answers or {}).get("company"), dict):
        vertical = answers["company"].get("vertical")
        industry = vertical if vertical in packs.INDUSTRIES else None
    if industry is not None:
        if industry not in packs.INDUSTRIES:
            raise StepError(f"Industria inválida: {industry}", status=422)
        run.industry = industry
    run.answers = deep_merge(run.answers or {}, answers or {})
    company = run.answers.get("company") or {}
    org = await session.get(Organization, run.organization_id)
    if run.industry:
        org.industry = run.industry
    if company.get("country"):
        org.country = str(company["country"]).upper()[:2]
    values = {k: company[k] for k in ("name", "timezone", "website", "address") if company.get(k)}
    if values:
        await set_setting(session, "company", values, run.organization_id, agent_id, source="system")
    if company.get("name") and run.industry:
        s = await step(session, run, "company")
        finish(s, "done", {"company": company.get("name"), "industry": run.industry})
    advance(run, await steps_of(session, run))
    if current_step is not None:  # el panel navega libremente entre pasos
        run.current_step = current_step
    return run


async def run_company(session: AsyncSession, run: OnboardingRun, body: dict | None,
                      agent_id: int | None) -> OnboardingStep:
    """steps/company/run: guarda las respuestas en la configuración y marca el paso."""
    body = body or {}
    await save_answers(session, run, body.get("industry"), body.get("answers") or {}, agent_id)
    s = await step(session, run, "company")
    company = (run.answers or {}).get("company") or {}
    if not company.get("name"):
        finish(s, "failed", error="Escribe el nombre de la empresa")
    else:
        finish(s, "done", {"company": company["name"], "industry": run.industry or "otro"})
        if not run.industry:
            run.industry = "otro"
    advance(run, await steps_of(session, run))
    return s


async def adopt_channel(session: AsyncSession, run: OnboardingRun) -> list[dict]:
    """steps/whatsapp/run: conexión manual (sin Embedded Signup): toma el número de WhatsApp más reciente."""
    channel = (await session.scalars(select(Channel).where(
        Channel.organization_id == run.organization_id, Channel.provider == "whatsapp_cloud")
        .order_by(Channel.id.desc()).limit(1))).first()
    if channel is None:
        raise StepError("No hay un número de WhatsApp: agrégalo con sus datos de Meta", blocking=["whatsapp"])
    return await after_connect(session, run, channel, {"manual": True})


# --- Paso: WhatsApp ---------------------------------------------------------------------
async def after_connect(session: AsyncSession, run: OnboardingRun, channel: Channel, detail: dict) -> list[dict]:
    run.channel_id = channel.id
    s = await step(session, run, "whatsapp")
    finish(s, "done", {"channel_id": channel.id, "display_phone": channel.display_phone, **detail})
    return await run_validate(session, run)


async def run_validate(session: AsyncSession, run: OnboardingRun) -> list[dict]:
    channel = await run_channel(session, run)
    s = await step(session, run, "validate")
    begin(s)
    results = await checks.run_checks(session, channel, run)
    by_key = {c["check_key"]: c for c in results}
    hard = [k for k in ("token_valid", "registered", "webhook") if by_key.get(k, {}).get("status") != "pass"]
    soft = [k for k, c in by_key.items() if c["status"] in ("warn", "fail") and k not in hard and k != "templates_ready"]
    if hard:
        finish(s, "failed", {"failing": hard}, "Hay validaciones por resolver: " + ", ".join(
            checks.CHECKS[k] for k in hard))
    else:
        finish(s, "warning" if soft else "done", {"warnings": soft})
    advance(run, await steps_of(session, run))
    return results


# --- Paso: perfil -------------------------------------------------------------------------
async def run_profile(session: AsyncSession, run: OnboardingRun, body: dict | None) -> OnboardingStep:
    channel = await run_channel(session, run)
    s = await step(session, run, "profile")
    begin(s)
    token = await meta.channel_token(session, channel)
    profile = meta.profile_from_answers(run.answers or {}, run.industry)
    overrides = {**((run.answers or {}).get("profile") or {}), **(body or {})}
    for k, limit in (("about", 139), ("description", 512), ("address", 256), ("email", 128)):
        if overrides.get(k):
            profile[k] = str(overrides[k])[:limit]
    if overrides.get("vertical"):
        profile["vertical"] = meta.VERTICALS.get(overrides["vertical"], overrides["vertical"])
    if overrides.get("websites"):
        profile["websites"] = [w for w in overrides["websites"] if w][:2]
    try:
        published = await meta.push_profile(session, channel, token, profile)
        finish(s, "done", {"profile": published})
    except graph.GraphError as e:
        finish(s, "failed", error=f"Meta: {e}")
    advance(run, await steps_of(session, run))
    return s


# --- Paso: plantillas ---------------------------------------------------------------------
async def submit_pack(session: AsyncSession, channel: Channel, run: OnboardingRun | None, template_keys: list[str] | None,
                      edits: dict, agent_id: int | None) -> dict:
    pk = packs.pack(run.industry if run else None, (run.answers if run else {}) or {})
    by_key = {t["template_key"]: t for t in pk["templates"]}
    keys = template_keys or [t["template_key"] for t in pk["templates"] if t["recommended"]]
    token = await meta.channel_token(session, channel)
    submitted, errors = [], []
    for key in keys:
        t = by_key.get(key)
        if t is None:
            errors.append({"template_key": key, "message": "No existe en el paquete"})
            continue
        t = {**t, **{k: v for k, v in (edits.get(key) or {}).items() if k in ("body", "header", "footer") and v}}
        try:
            row = await meta.submit_template(session, channel, token, t, pack_key=pk["pack_key"], agent_id=agent_id)
            submitted.append({"template_key": key, "name": row.name, "status": row.status,
                              "meta_template_id": row.meta_template_id})
        except graph.GraphError as e:
            errors.append({"template_key": key, "message": str(e)})
    if run is not None:
        s = await step(session, run, "templates")
        status = "done" if submitted and not errors else ("warning" if submitted else "failed")
        await refresh_templates_result(session, run, channel)
        finish(s, status, {"pack_key": pk["pack_key"]},
               "; ".join(f"{e['template_key']}: {e['message']}" for e in errors) or None)
        advance(run, await steps_of(session, run))
    return {"submitted": submitted, "errors": errors}


async def refresh_templates_result(session: AsyncSession, run: OnboardingRun, channel: Channel | None) -> None:
    """steps[templates].result.submitted = plantillas del asistente con su estado actual."""
    if channel is None or not channel.waba_id:
        return
    rows = (await session.scalars(select(WaTemplate).where(
        WaTemplate.organization_id == run.organization_id, WaTemplate.waba_id == channel.waba_id,
        WaTemplate.source == "onboarding").order_by(WaTemplate.submitted_at))).all()
    if not rows:
        return
    s = await step(session, run, "templates")
    s.result = {**(s.result or {}), "submitted": [
        {"template_key": r.template_key, "name": r.name, "status": r.status, "meta_template_id": r.meta_template_id,
         "rejected_reason": r.rejected_reason, "category": r.category} for r in rows]}


REWRITE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["body", "category", "example_values"],
                  "properties": {"body": {"type": "string"}, "category": {"type": "string",
                                                                          "enum": ["UTILITY", "MARKETING"]},
                                 "example_values": {"type": "array", "items": {"type": "string"}}}}


async def rewrite_rejected(session: AsyncSession, run: OnboardingRun, template_key: str,
                           agent_id: int | None) -> dict:
    channel = await run_channel(session, run)
    rows = (await session.scalars(select(WaTemplate).where(
        WaTemplate.organization_id == run.organization_id, WaTemplate.template_key == template_key,
        WaTemplate.source == "onboarding").order_by(WaTemplate.submitted_at.desc()))).all()
    row = next((r for r in rows if r.status == "REJECTED"), None)
    if row is None:
        raise StepError("Esa plantilla no está rechazada", status=409)
    parts = packs.from_components(row.components)
    system = ("Reescribes plantillas de WhatsApp rechazadas por Meta para que sean aprobadas. Mantén el propósito y "
              "las mismas variables {{1}}, {{2}}… en orden (el texto no puede empezar ni terminar con una variable). "
              "UTILITY solo si el mensaje es sobre una solicitud, cita, pedido o servicio del cliente y sin promoción; "
              "si no, MARKETING. Español neutro, máximo 1024 caracteres, sin URLs acortadas ni mayúsculas sostenidas.")
    user = (f"Plantilla: {row.name} ({row.category})\nTexto:\n{parts['body']}\nEjemplos: {parts['example_values']}\n"
            f"Motivo del rechazo de Meta: {row.rejected_reason or 'no indicado'}")
    data, _ctx = await json_call(run.organization_id, None, "onboarding", system, user, REWRITE_SCHEMA)
    base = row.name.rsplit("_v", 1)[0] if row.name.rsplit("_v", 1)[-1].isdigit() else row.name
    version = 2
    names = {r.name for r in rows}
    while f"{base}_v{version}" in names:
        version += 1
    t = {"template_key": template_key, "name": f"{base}_v{version}", "category": data["category"],
         "language": row.language, "body": data["body"].strip(), "example_values": data.get("example_values") or
         parts["example_values"], "header": parts["header"], "footer": parts["footer"], "buttons": parts["buttons"]}
    token = await meta.channel_token(session, channel)
    try:
        new = await meta.submit_template(session, channel, token, t, pack_key=row.pack_key, agent_id=agent_id)
    except graph.GraphError as e:
        return {"submitted": [], "errors": [{"template_key": template_key, "message": str(e)}]}
    await refresh_templates_result(session, run, channel)
    return {"submitted": [{"template_key": template_key, "name": new.name, "status": new.status,
                           "meta_template_id": new.meta_template_id}], "errors": []}


# --- Paso: agente de IA y ajustes -----------------------------------------------------------
TONES = {None: "cercano, claro y profesional"}


def build_prompt(org_name: str, industry: str | None, answers: dict) -> str:
    c = answers.get("company") or {}
    imported_profile = imported(answers).get("profile") or {}
    tone = c.get("tone") or imported_profile.get("tone") or TONES[None]
    desc = c.get("description") or imported_profile.get("description") or ""
    hours = answers.get("hours") or {}
    lines = [
        f"Eres el asistente virtual de {org_name} en WhatsApp ({packs.INDUSTRY_LABELS.get(packs.industry_or_default(industry))}).",
        f"Tono: {tone}. Respuestas breves (máximo 3 frases), en el idioma del cliente.",
    ]
    if desc:
        lines.append(f"Sobre la empresa: {desc}")
    if c.get("website") or imported_profile.get("website"):
        lines.append(f"Sitio web: {c.get('website') or imported_profile.get('website')}")
    if c.get("address") or imported_profile.get("address"):
        lines.append(f"Dirección: {c.get('address') or imported_profile.get('address')}")
    if hours.get("start") and hours.get("end"):
        lines.append(f"Horario de atención con asesores: {hours['start']} a {hours['end']}.")
    if c.get("goals"):
        lines.append(f"Objetivo de las conversaciones: {c['goals']}")
    lines += [
        "Usa el conocimiento y el catálogo de la empresa; si no sabes algo, dilo y ofrece pasar con un asesor.",
        "Nunca inventes precios, disponibilidad ni políticas.",
        "Transfiere a un asesor cuando el cliente lo pida, quiera comprar o agendar, o tenga un reclamo.",
    ]
    return "\n".join(lines)


async def run_ai(session: AsyncSession, run: OnboardingRun) -> OnboardingStep:
    from app.settings_store import set_setting

    org = run.organization_id
    s = await step(session, run, "ai")
    begin(s)
    o = await session.get(Organization, org)
    answers = run.answers or {}
    agent = (await session.scalars(select(AIAgent).where(AIAgent.organization_id == org)
                                   .order_by(AIAgent.id).limit(1))).first()
    if agent is None:
        from app.tenancy import provision_ai_defaults

        agent = await provision_ai_defaults(session, org)
    on = ai_toggles(answers)
    if on["apply_prompt"]:
        agent.system_prompt = build_prompt(o.name, run.industry, answers)
        agent.updated_at = utcnow()

    created_docs = 0
    faqs = imported(answers).get("faqs") or []
    if faqs and on["apply_knowledge"]:
        title = "Preguntas frecuentes (sitio web)"
        content = "\n\n".join(f"P: {f['question']}\nR: {f['answer']}" for f in faqs)
        doc = (await session.scalars(select(KnowledgeDoc).where(
            KnowledgeDoc.organization_id == org, KnowledgeDoc.title == title).limit(1))).first()
        if doc is None:
            doc = KnowledgeDoc(organization_id=org, title=title, content=content, source_filename="onboarding")
            session.add(doc)
            await session.flush()
            created_docs = 1
        else:
            doc.content = content
        if not await session.get(AIAgentKnowledge, (agent.id, doc.id)):
            session.add(AIAgentKnowledge(ai_agent_id=agent.id, doc_id=doc.id))

    preset = packs.PRESETS[packs.industry_or_default(run.industry)]
    if on["apply_typifications"]:
        await set_setting(session, "conversations", {"typifications": preset["typifications"]}, org, None,
                          source="system")
    groups: list[str] = []
    if on["apply_groups"]:
        existing = {g.name for g in (await session.scalars(select(Group).where(Group.organization_id == org))).all()}
        groups = [name for name, _d in preset["groups"] if name not in existing]
        for name, desc in preset["groups"]:
            if name in groups:
                session.add(Group(organization_id=org, name=name, description=desc))

    try:  # etapas por línea de negocio con condición para la IA (§17)
        from app.agent_config import provision_default_stages

        await provision_default_stages(session, org, run.industry)
    except Exception:  # noqa: BLE001 — opcional: el paso no falla por esto
        logging.getLogger(__name__).exception("No se pudieron crear las etapas por defecto")
    products_added = 0
    if on["apply_catalog"]:
        products_added = await import_products(session, org, imported(answers).get("products") or [])

    hours = hours_config(answers)
    custom_msg = ((answers.get("ai") or {}).get("hours_message") or (answers.get("hours") or {}).get("message"))
    if hours and on["apply_hours_message"]:
        rule = (await session.scalars(select(Automation).where(
            Automation.organization_id == org, Automation.type == "business_hours").limit(1))).first()
        cfg = {**hours, "message": custom_msg or "Gracias por escribirnos. Nuestro horario de atención es de "
                                                 f"{hours['start']} a {hours['end']}; te responderemos apenas abramos."}
        if rule is None:
            session.add(Automation(organization_id=org, name="Fuera de horario", type="business_hours", config=cfg))
        else:
            rule.config = cfg
    finish(s, "done", {"ai_agent_id": agent.id, "knowledge_docs": created_docs, "groups_created": groups,
                       "typifications": preset["typifications"] if on["apply_typifications"] else [],
                       "products_added": products_added, "applied": [k for k, v in on.items() if v]})
    advance(run, await steps_of(session, run))
    return s


def packs_slug(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


async def import_products(session: AsyncSession, org: int, products: list[dict]) -> int:
    """Productos del sitio web al catálogo (solo los que aún no existen por nombre)."""
    from app.models import Product

    if not products:
        return 0
    taken = set((await session.scalars(select(Product.sku).where(Product.organization_id == org))).all())
    names = {n.lower() for n in (await session.scalars(select(Product.name).where(
        Product.organization_id == org))).all()}
    added = 0
    for p in products[:30]:
        name = (p.get("name") or "").strip()
        sku = "web-" + (re.sub(r"[^a-z0-9]+", "-", packs_slug(name)).strip("-")[:40] or "producto")
        if not name or name.lower() in names or sku in taken:
            continue
        currency = (p.get("currency") or "").strip().upper()
        session.add(Product(organization_id=org, sku=sku, name=name[:200], description=p.get("description"),
                            price=p.get("price"), url=p.get("url"), image_url=p.get("image_url"), source="import",
                            **({"currency": currency} if re.fullmatch(r"[A-Z]{3}", currency) else {})))
        names.add(name.lower())
        taken.add(sku)
        added += 1
    return added


# --- Paso: equipo, prueba y salida en vivo ------------------------------------------------------
async def run_team(session: AsyncSession, run: OnboardingRun) -> OnboardingStep:
    s = await step(session, run, "team")
    sent = (await session.scalars(select(AgentInvitation.id).where(
        AgentInvitation.organization_id == run.organization_id))).all()
    finish(s, "done" if sent else "skipped", {"invitations": len(sent)})
    advance(run, await steps_of(session, run))
    return s


async def send_test(session: AsyncSession, run: OnboardingRun, phone: str) -> dict:
    phone = "".join(ch for ch in phone if ch.isdigit())
    if not 8 <= len(phone) <= 15:
        raise StepError("Escribe el número con indicativo de país, solo dígitos (ej. 573001234567)", status=422)
    channel = await run_channel(session, run)
    token = await meta.channel_token(session, channel)
    approved = (await session.scalars(select(WaTemplate).where(
        WaTemplate.organization_id == run.organization_id, WaTemplate.waba_id == channel.waba_id,
        WaTemplate.status == "APPROVED", WaTemplate.source == "onboarding",
        WaTemplate.category == "UTILITY").limit(1))).first()
    s = await step(session, run, "test")
    begin(s)
    try:
        message_id = await meta.send_test(channel, token, phone, approved)
    except graph.GraphError as e:
        finish(s, "failed", error=f"Meta: {e}")
        return {"sent": False, "message_id": None, "waiting_reply": False, "error": str(e)}
    run.answers = deep_merge(run.answers or {}, {"test": {"phone": phone, "message_id": message_id,
                                                          "sent_at": utcnow().isoformat(), "replied": False,
                                                          "template": approved.name if approved else "hello_world"}})
    s.status, s.result = "running", {"phone": phone, "message_id": message_id}
    await checks.run_checks(session, channel, run)
    return {"sent": True, "message_id": message_id, "waiting_reply": True}


async def go_live(session: AsyncSession, run: OnboardingRun) -> dict:
    steps = await steps_of(session, run)
    channel = await run_channel(session, run)
    results = {c["check_key"]: c for c in await checks.run_checks(session, channel, run)}
    blocking = [k for k in ("company", "whatsapp") if steps.get(k) is None or steps[k].status not in ("done", "warning")]
    blocking += [k for k in checks.REQUIRED_FOR_GO_LIVE
                 if results.get(k, {}).get("status") not in ("pass", "warn")]
    blocking = list(dict.fromkeys(blocking))
    if blocking:
        labels = [checks.CHECKS.get(k) or next((x[1] for x in STEPS if x[0] == k), k) for k in blocking]
        raise StepError("Falta resolver: " + ", ".join(labels), blocking=blocking)
    now = utcnow()
    org = await session.get(Organization, run.organization_id)
    org.onboarding_completed_at = now
    agent = (await session.scalars(select(AIAgent).where(AIAgent.organization_id == run.organization_id)
                                   .order_by(AIAgent.id).limit(1))).first()
    if agent is not None:
        agent.enabled = True
        if channel.default_ai_agent_id is None:
            channel.default_ai_agent_id = agent.id
    channel.status = "active"
    s = steps.get("go_live") or await step(session, run, "go_live")
    finish(s, "done", {"warnings": [k for k, c in results.items() if c["status"] == "warn"]})
    run.status, run.completed_at, run.current_step = "completed", now, "go_live"
    return {"completed_at": now, "warnings": s.result.get("warnings", [])}


async def state(session: AsyncSession, org: int, agent_id: int | None) -> dict:
    env = get_settings()
    run = await active_run(session, org, create=True, agent_id=agent_id) or await latest_run(session, org)
    if run is not None and run.channel_id:
        await refresh_templates_result(session, run, await session.get(Channel, run.channel_id))
    steps = await steps_of(session, run) if run else {}
    o = await session.get(Organization, org)
    return {
        "run": run_out(run),
        "steps": [step_out(steps[k]) for k in STEP_KEYS if k in steps],
        "checks": await checks.list_checks(session, run.channel_id if run else None),
        "org": {"name": o.name, "industry": o.industry, "onboarding_completed_at": o.onboarding_completed_at},
        "embedded_signup": {"enabled": bool(env.meta_app_id and env.meta_app_secret
                                            and env.meta_embedded_signup_config_id),
                            "app_id": env.meta_app_id or None, "config_id": env.meta_embedded_signup_config_id or None,
                            "api_version": env.wa_api_version},
    }
