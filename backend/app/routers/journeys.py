"""Journeys de marketing: CRUD, versiones, publicar / pausar / archivar, inscripciones y reporte (§21.1).

También el redireccionador de clics `/j/c?t=…` (enlaces {{link:…}} de los mensajes del journey).
"""

import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session
from app.journeys import engine, schema
from app.models import Agent, Contact, Journey, JourneyEnrollment, JourneyVersion, Segment, utcnow
from app.schemas import UTCDateTime

router = APIRouter(tags=["journeys"])
P = "/api/journeys"


class JourneyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    entry: dict = {"type": "manual"}
    settings: dict = {}
    definition: dict | None = None  # al crear: primera versión


class VersionIn(BaseModel):
    definition: dict
    change_note: str | None = None


class PublishIn(BaseModel):
    version_id: int | None = None


class EnrollIn(BaseModel):
    contact_ids: list[int] = Field(min_length=1, max_length=5000)


class JourneyOut(BaseModel):
    id: int
    name: str
    description: str | None
    status: str
    entry: dict
    settings: dict
    current_version_id: int | None
    published_at: UTCDateTime | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    stats: dict = {}


def _out(j: Journey, stats: dict | None = None) -> JourneyOut:
    return JourneyOut(id=j.id, name=j.name, description=j.description, status=j.status, entry=j.entry or {},
                      settings=j.settings or {}, current_version_id=j.current_version_id,
                      published_at=j.published_at, created_at=j.created_at, updated_at=j.updated_at,
                      stats=stats or {})


async def _journey(session: AsyncSession, journey_id: int, org: int) -> Journey:
    j = await session.get(Journey, journey_id)
    if j is None or j.organization_id != org:
        raise HTTPException(404, "Journey no encontrado")
    return j


async def _latest(session: AsyncSession, journey_id: int) -> JourneyVersion | None:
    return await session.scalar(select(JourneyVersion).where(JourneyVersion.journey_id == journey_id)
                                .order_by(JourneyVersion.version.desc()).limit(1))


async def _stats(session: AsyncSession, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {i: {} for i in ids}
    if not ids:
        return out
    rows = (await session.execute(select(JourneyEnrollment.journey_id, JourneyEnrollment.status, func.count())
                                  .where(JourneyEnrollment.journey_id.in_(ids))
                                  .group_by(JourneyEnrollment.journey_id, JourneyEnrollment.status))).all()
    for jid, status, n in rows:
        out[jid][status] = n
        out[jid]["enrolled"] = out[jid].get("enrolled", 0) + n
    return out


def _check_config(entry: dict, settings: dict) -> list[str]:
    return schema.validate_entry(entry) + schema.validate_settings(settings)


async def _check_refs(session: AsyncSession, org: int, entry: dict, definition: dict | None) -> list[str]:
    """Referencias que existen en la empresa: segmento de entrada, campo de fecha, flujos, plantillas aprobadas."""
    errs: list[str] = []
    if entry.get("segment_id"):
        seg = await session.get(Segment, int(entry["segment_id"]))
        if seg is None or seg.organization_id != org:
            errs.append("El segmento de entrada no existe")
    if entry.get("type") == "date_field" and entry.get("field"):
        from app import segments

        types = {f["key"]: f["type"] for f in await segments.field_catalog(session, org)}
        if types.get(entry["field"]) not in ("date", "ts"):
            errs.append(f"{entry['field']} no es un campo de fecha")
    for s in (definition or {}).get("steps") or []:
        cfg = s.get("config") or {}
        if s["type"] == "start_flow" and cfg.get("flow_id"):
            from app.models import Flow

            flow = await session.get(Flow, int(cfg["flow_id"]))
            if flow is None or flow.organization_id != org:
                errs.append(f"{s['id']}: el flujo no existe")
        if s["type"] == "branch" and (cfg.get("condition") or {}).get("type") == "field":
            from app import segments

            try:
                await segments.compile_definition(session, org, cfg["condition"].get("rule") or {})
            except segments.SegmentError as e:
                errs.append(f"{s['id']}: {e}")
    errs += await _check_templates(session, org, definition)
    return errs


async def _check_templates(session: AsyncSession, org: int, definition: dict | None) -> list[str]:
    wanted = []
    for s in (definition or {}).get("steps") or []:
        cfg = s.get("config") or {}
        if s["type"] == "send_template":
            wanted.append((s["id"], cfg.get("template_name"), cfg.get("language")))
        fb = cfg.get("fallback_template") or {}
        if s["type"] == "send_text" and fb.get("template_name"):
            wanted.append((s["id"], fb.get("template_name"), fb.get("language")))
    if not wanted:
        return []
    from app import templates
    from app.models import Channel

    channel = (await session.scalars(select(Channel).where(Channel.organization_id == org,
                                                           Channel.provider == "whatsapp_cloud")
                                     .order_by(Channel.id).limit(1))).first()
    if channel is None:
        return ["No hay un número de WhatsApp para enviar plantillas"]
    try:
        catalog = await templates.list_templates(session, channel)
    except Exception:  # noqa: BLE001 — sin acceso a Meta: se valida al enviar
        return []
    approved = {(t["name"], t["language"]) for t in catalog if t.get("status") == "APPROVED"}
    return [f"{sid}: la plantilla {name} ({lang}) no existe o no está aprobada"
            for sid, name, lang in wanted if (name, lang) not in approved]


# --- Catálogo ---------------------------------------------------------------------------------------------------
@router.get(P + "/meta")
async def meta(agent: Agent = Depends(current_agent)):
    return {"step_types": schema.STEP_TYPES, "events": schema.EVENTS, "goals": list(schema.GOALS),
            "entry_types": list(schema.ENTRY_TYPES), "branch_conditions": list(schema.BRANCH_CONDITIONS)}


@router.post(P + "/validate")
async def validate(body: VersionIn, agent: Agent = Depends(current_agent)):
    """Errores del grafo (el editor los muestra en línea por paso)."""
    return {"errors": schema.validate_definition(body.definition)}


# --- CRUD ---------------------------------------------------------------------------------------------------------
@router.get(P, response_model=list[JourneyOut])
async def list_journeys(status: str | None = None, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    q = select(Journey).where(Journey.organization_id == agent.organization_id)
    if status:
        q = q.where(Journey.status == status)
    rows = (await session.scalars(q.order_by(Journey.updated_at.desc()))).all()
    stats = await _stats(session, [j.id for j in rows])
    return [_out(j, stats[j.id]) for j in rows]


@router.post(P, response_model=JourneyOut)
async def create_journey(body: JourneyIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    errs = _check_config(body.entry, body.settings)
    if errs:
        raise HTTPException(422, {"message": "Configuración inválida", "errors": errs})
    if await session.scalar(select(Journey.id).where(Journey.organization_id == org, Journey.name == body.name.strip())):
        raise HTTPException(409, "Ya existe un journey con ese nombre")
    j = Journey(organization_id=org, name=body.name.strip(), description=body.description, status="draft",
                entry=body.entry, settings=body.settings, created_by=agent.id)
    session.add(j)
    await session.flush()
    if body.definition:
        session.add(JourneyVersion(journey_id=j.id, version=1, definition=body.definition, created_by=agent.id))
    await session.commit()
    return _out(j)


@router.get(P + "/{journey_id}")
async def get_journey(journey_id: int, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    j = await _journey(session, journey_id, agent.organization_id)
    latest = await _latest(session, j.id)
    versions = (await session.execute(select(JourneyVersion.id, JourneyVersion.version, JourneyVersion.change_note,
                                             JourneyVersion.created_at)
                                      .where(JourneyVersion.journey_id == j.id)
                                      .order_by(JourneyVersion.version.desc()))).all()
    definition = latest.definition if latest else None
    return {"journey": _out(j, (await _stats(session, [j.id]))[j.id]),
            "version": {"id": latest.id, "version": latest.version, "definition": definition} if latest else None,
            "errors": schema.validate_definition(definition) if definition else ["El journey no tiene pasos"],
            "versions": [{"id": v.id, "version": v.version, "change_note": v.change_note, "created_at": v.created_at,
                          "current": v.id == j.current_version_id} for v in versions]}


@router.put(P + "/{journey_id}", response_model=JourneyOut)
async def update_journey(journey_id: int, body: JourneyIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    j = await _journey(session, journey_id, agent.organization_id)
    errs = _check_config(body.entry, body.settings)
    if errs:
        raise HTTPException(422, {"message": "Configuración inválida", "errors": errs})
    if j.status == "active" and (body.entry or {}).get("type") != (j.entry or {}).get("type"):
        raise HTTPException(409, "Pausa el journey para cambiar el tipo de entrada")
    dup = await session.scalar(select(Journey.id).where(Journey.organization_id == j.organization_id,
                                                        Journey.name == body.name.strip(), Journey.id != j.id))
    if dup:
        raise HTTPException(409, "Ya existe un journey con ese nombre")
    j.name, j.description, j.entry, j.settings = body.name.strip(), body.description, body.entry, body.settings
    if body.definition:
        latest = await _latest(session, j.id)
        if latest is None or latest.definition != body.definition:
            session.add(JourneyVersion(journey_id=j.id, version=(latest.version if latest else 0) + 1,
                                       definition=body.definition, created_by=agent.id))
    await session.commit()
    return _out(j)


@router.delete(P + "/{journey_id}")
async def delete_journey(journey_id: int, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    j = await _journey(session, journey_id, agent.organization_id)
    if j.status != "draft" or await session.scalar(select(JourneyEnrollment.id).where(
            JourneyEnrollment.journey_id == j.id).limit(1)):
        raise HTTPException(409, "Solo se eliminan borradores sin inscripciones; archívalo")
    await session.delete(j)
    await session.commit()
    return {"ok": True}


# --- Versiones ----------------------------------------------------------------------------------------------------
@router.post(P + "/{journey_id}/versions")
async def save_version(journey_id: int, body: VersionIn, agent: Agent = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    """Guarda el borrador como nueva versión (aunque tenga errores: se muestran y bloquean la publicación)."""
    j = await _journey(session, journey_id, agent.organization_id)
    latest = await _latest(session, j.id)
    v = JourneyVersion(journey_id=j.id, version=(latest.version if latest else 0) + 1, definition=body.definition,
                       change_note=body.change_note, created_by=agent.id)
    session.add(v)
    await session.commit()
    return {"id": v.id, "version": v.version, "errors": schema.validate_definition(body.definition)}


@router.get(P + "/{journey_id}/versions/{version_id}")
async def get_version(journey_id: int, version_id: int, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    j = await _journey(session, journey_id, agent.organization_id)
    v = await session.get(JourneyVersion, version_id)
    if v is None or v.journey_id != j.id:
        raise HTTPException(404, "Versión no encontrada")
    return {"id": v.id, "version": v.version, "definition": v.definition, "change_note": v.change_note,
            "created_at": v.created_at}


@router.post(P + "/{journey_id}/publish")
async def publish(journey_id: int, body: PublishIn | None = None, agent: Agent = Depends(require_admin),
                  session: AsyncSession = Depends(get_session)):
    """Publica una versión (la última si no se indica) y activa el journey. Las inscripciones en curso siguen en
    su versión; las nuevas usan esta."""
    from app.journeys.scheduler import on_publish

    j = await _journey(session, journey_id, agent.organization_id)
    if j.status == "archived":
        raise HTTPException(409, "El journey está archivado")
    v = await session.get(JourneyVersion, body.version_id) if body and body.version_id else await _latest(session, j.id)
    if v is None or v.journey_id != j.id:
        raise HTTPException(404, "Versión no encontrada")
    errs = schema.validate_definition(v.definition) + _check_config(j.entry or {}, j.settings or {})
    if not errs:
        errs = await _check_refs(session, j.organization_id, j.entry or {}, v.definition)
    if errs:
        raise HTTPException(422, {"message": "El journey tiene errores", "errors": errs})
    first = j.current_version_id is None
    j.current_version_id, j.status, j.published_at = v.id, "active", utcnow()
    await session.commit()
    enrolled = await on_publish(session, j) if first else 0
    return {"journey": _out(j), "enrolled": enrolled}


@router.post(P + "/{journey_id}/pause", response_model=JourneyOut)
async def pause(journey_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await _set_status(session, journey_id, "pause", agent)


@router.post(P + "/{journey_id}/resume", response_model=JourneyOut)
async def resume(journey_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await _set_status(session, journey_id, "resume", agent)


@router.post(P + "/{journey_id}/archive", response_model=JourneyOut)
async def archive(journey_id: int, agent: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    return await _set_status(session, journey_id, "archive", agent)


async def _set_status(session: AsyncSession, journey_id: int, action: str, agent: Agent) -> JourneyOut:
    j = await _journey(session, journey_id, agent.organization_id)
    if action == "pause":
        if j.status != "active":
            raise HTTPException(409, "Solo se pausa un journey activo")
        j.status = "paused"
    elif action == "resume":
        if j.status != "paused":
            raise HTTPException(409, "El journey no está en pausa")
        j.status = "active"
        # Todas se reevalúan en el próximo ciclo: las esperas vuelven a esperar su plazo guardado (idempotente)
        await session.execute(text(
            "update public.journey_enrollments set next_run_at = now() where journey_id = :j "
            "and status in ('active', 'waiting')"), {"j": j.id})
    elif action == "archive":
        j.status = "archived"
        rows = (await session.scalars(select(JourneyEnrollment).where(
            JourneyEnrollment.journey_id == j.id, JourneyEnrollment.status.in_(engine.ACTIVE)))).all()
        for e in rows:
            await engine.finish(session, e, "exited", "journey_archived")
    await session.commit()
    return _out(j)


# --- Inscripciones -------------------------------------------------------------------------------------------------
@router.post(P + "/{journey_id}/enroll")
async def enroll_manual(journey_id: int, body: EnrollIn, agent: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    j = await _journey(session, journey_id, agent.organization_id)
    if j.status != "active":
        raise HTTPException(409, "Publica el journey antes de inscribir clientes")
    ids = set((await session.scalars(select(Contact.id).where(
        Contact.organization_id == j.organization_id, Contact.id.in_(body.contact_ids)))).all())
    enrolled = []
    for cid in sorted(ids):
        e = await engine.enroll(session, j, cid, "manual", {"agent_id": agent.id})
        if e:
            enrolled.append(cid)
    return {"enrolled": len(enrolled), "skipped": len(body.contact_ids) - len(enrolled), "contact_ids": enrolled}


@router.get(P + "/{journey_id}/enrollments")
async def list_enrollments(journey_id: int, status: str | None = None, limit: int = Query(50, le=500),
                           offset: int = 0, agent: Agent = Depends(current_agent),
                           session: AsyncSession = Depends(get_session)):
    j = await _journey(session, journey_id, agent.organization_id)
    q = select(JourneyEnrollment, Contact.name, Contact.wa_id).join(
        Contact, Contact.id == JourneyEnrollment.contact_id).where(JourneyEnrollment.journey_id == j.id)
    if status:
        q = q.where(JourneyEnrollment.status == status)
    rows = (await session.execute(q.order_by(JourneyEnrollment.enrolled_at.desc()).offset(offset).limit(limit))).all()
    return [{"id": e.id, "contact_id": e.contact_id, "contact_name": name, "contact_phone": phone, "status": e.status,
             "current_step": e.current_step, "variant": e.variant, "next_run_at": e.next_run_at,
             "exit_reason": e.exit_reason, "enrolled_at": e.enrolled_at, "finished_at": e.finished_at,
             "source": (e.context or {}).get("source")} for e, name, phone in rows]


# --- Reporte -------------------------------------------------------------------------------------------------------
def z_test(n1: int, c1: int, n2: int, c2: int) -> dict:
    """Prueba z de dos proporciones (bilateral)."""
    if not n1 or not n2:
        return {"z": None, "p_value": None, "significant": False}
    p1, p2 = c1 / n1, c2 / n2
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return {"z": 0.0, "p_value": 1.0, "significant": False}
    z = (p2 - p1) / se
    pv = math.erfc(abs(z) / math.sqrt(2))
    return {"z": round(z, 3), "p_value": round(pv, 4), "significant": pv < 0.05}


@router.get(P + "/{journey_id}/report")
async def report(journey_id: int, agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Embudo por paso (inscripciones distintas por tipo de evento) y A/B por cada prueba con lift y prueba z."""
    j = await _journey(session, journey_id, agent.organization_id)
    version = await session.get(JourneyVersion, j.current_version_id) if j.current_version_id else await _latest(
        session, j.id)
    definition = version.definition if version else {"steps": []}
    rows = (await session.execute(text(
        "select step_id, kind, count(distinct enrollment_id) from public.journey_events where journey_id = :j "
        "group by 1, 2"), {"j": j.id})).all()
    counts: dict[str, dict] = {}
    for step_id, kind, n in rows:
        counts.setdefault(step_id, {})[kind] = n
    steps = [{"id": s["id"], "type": s["type"], "label": schema.STEP_TYPES.get(s["type"], s["type"]),
              **{k: counts.get(s["id"], {}).get(k, 0) for k in
                 ("sent", "delivered", "read", "replied", "clicked", "failed", "skipped", "waited", "branched")}}
             for s in schema.ordered_steps(definition)] if definition.get("steps") else []
    conv_kind = "goal_met" if (j.settings or {}).get("goal") else "replied"
    ab = []
    for s in definition.get("steps") or []:
        if s["type"] != "split":
            continue
        vrows = (await session.execute(text(
            "select ev.data->>'variant' as v, count(distinct ev.enrollment_id) as n, "
            "count(distinct ev.enrollment_id) filter (where exists (select 1 from public.journey_events g "
            "  where g.enrollment_id = ev.enrollment_id and g.journey_id = :j and g.kind = :k)) as c "
            "from public.journey_events ev where ev.journey_id = :j and ev.step_id = :s and ev.kind = 'branched' "
            "group by 1 order by 1"), {"j": j.id, "s": s["id"], "k": conv_kind})).all()
        by = {r.v: (r.n, r.c) for r in vrows}
        keys = [v["key"] for v in (s.get("config") or {}).get("variants") or []]
        base = by.get(keys[0], (0, 0)) if keys else (0, 0)
        variants = []
        for i, k in enumerate(keys):
            n, c = by.get(k, (0, 0))
            rate = c / n if n else 0.0
            base_rate = base[1] / base[0] if base[0] else 0.0
            item = {"key": k, "enrolled": n, "conversions": c, "rate": round(rate, 4)}
            if i > 0:
                item["lift"] = round((rate - base_rate) / base_rate, 4) if base_rate else None
                item.update(z_test(base[0], base[1], n, c))
            variants.append(item)
        ab.append({"step_id": s["id"], "metric": conv_kind, "variants": variants})
    totals = (await _stats(session, [j.id]))[j.id]
    return {"journey_id": j.id, "totals": totals, "steps": steps, "ab": ab, "conversion_metric": conv_kind,
            "generated_at": datetime.now(UTC)}


# --- Clics ---------------------------------------------------------------------------------------------------------
@router.get("/j/c", include_in_schema=False)
async def click(t: str, session: AsyncSession = Depends(get_session)):
    """Registra el clic (rama «hizo clic») y redirige al destino. El token va firmado: no es un redirector abierto."""
    parsed = engine.parse_click_token(t)
    if parsed is None:
        raise HTTPException(404, "Enlace inválido")
    eid, enrolled_at, step_id, url = parsed
    e = await engine.get_enrollment(session, eid, enrolled_at)
    if e is not None:
        await engine.add_event(session, e, step_id, "clicked", data={"url": url[:500]})
        if e.status == "waiting":
            e.next_run_at = utcnow()
        await session.commit()
        if e.status == "waiting":
            await engine.schedule_run(session, e)
    return RedirectResponse(url, status_code=302)
