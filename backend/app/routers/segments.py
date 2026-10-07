"""Segmentos: reglas sobre el cliente 360 (dinámicos) o listas fijas (estáticos) (§21.1)."""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import segments
from app.auth import current_agent, require_admin
from app.db import get_session
from app.models import Agent, Contact, Journey, Segment, SegmentMember
from app.schemas import UTCDateTime

router = APIRouter(prefix="/api/segments", tags=["segments"])


class SegmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    kind: str = "dynamic"
    definition: dict = {}
    refresh_minutes: int = Field(60, ge=5, le=10080)
    contact_ids: list[int] = []  # estáticos: miembros


class SegmentOut(BaseModel):
    id: int
    name: str
    description: str | None
    kind: str
    definition: dict
    member_count: int
    last_computed_at: UTCDateTime | None
    refresh_minutes: int
    created_at: UTCDateTime
    updated_at: UTCDateTime
    journeys: list[dict] = []


class PreviewIn(BaseModel):
    definition: dict


def _out(s: Segment, journeys: list[Journey] | None = None) -> SegmentOut:
    return SegmentOut(id=s.id, name=s.name, description=s.description, kind=s.kind, definition=s.definition or {},
                      member_count=s.member_count, last_computed_at=s.last_computed_at,
                      refresh_minutes=s.refresh_minutes, created_at=s.created_at, updated_at=s.updated_at,
                      journeys=[{"id": j.id, "name": j.name, "status": j.status} for j in journeys or []])


async def _segment(session: AsyncSession, segment_id: int, org: int) -> Segment:
    s = await session.get(Segment, segment_id)
    if s is None or s.organization_id != org:
        raise HTTPException(404, "Segmento no encontrado")
    return s


async def _using(session: AsyncSession, org: int, segment_id: int) -> list[Journey]:
    rows = (await session.scalars(select(Journey).where(Journey.organization_id == org,
                                                        Journey.status != "archived"))).all()
    return [j for j in rows if int((j.entry or {}).get("segment_id") or 0) == segment_id]


async def _validate(session: AsyncSession, org: int, body: SegmentIn) -> None:
    if body.kind not in ("dynamic", "static"):
        raise HTTPException(422, "Tipo inválido (dynamic o static)")
    if body.kind == "dynamic":
        if not body.definition:
            raise HTTPException(422, "Agrega al menos una condición")
        try:
            await segments.compile_definition(session, org, body.definition)
        except segments.SegmentError as e:
            raise HTTPException(422, str(e)) from e


async def _set_static_members(session: AsyncSession, s: Segment, contact_ids: list[int]) -> None:
    ids = set((await session.scalars(select(Contact.id).where(
        Contact.organization_id == s.organization_id, Contact.id.in_(contact_ids or [0])))).all())
    await session.execute(delete(SegmentMember).where(SegmentMember.segment_id == s.id))
    if ids:
        await session.execute(insert(SegmentMember), [{"segment_id": s.id, "contact_id": c} for c in sorted(ids)])
    s.member_count = len(ids)


@router.get("/fields")
async def fields(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Campos permitidos (con su tipo, operadores y opciones) para el constructor de reglas."""
    return {"fields": await segments.field_catalog(session, agent.organization_id),
            "ops": {k: list(v) for k, v in segments.OPS_BY_TYPE.items()}}


@router.get("", response_model=list[SegmentOut])
async def list_segments(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(Segment).where(Segment.organization_id == agent.organization_id)
                                  .order_by(Segment.name))).all()
    return [_out(s) for s in rows]


@router.post("", response_model=SegmentOut)
async def create_segment(body: SegmentIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    org = agent.organization_id
    await _validate(session, org, body)
    if await session.scalar(select(Segment.id).where(Segment.organization_id == org, Segment.name == body.name.strip())):
        raise HTTPException(409, "Ya existe un segmento con ese nombre")
    s = Segment(organization_id=org, name=body.name.strip(), description=body.description, kind=body.kind,
                definition=body.definition if body.kind == "dynamic" else {}, refresh_minutes=body.refresh_minutes,
                created_by=agent.id)
    session.add(s)
    await session.flush()
    if body.kind == "static":
        await _set_static_members(session, s, body.contact_ids)
    await session.commit()
    if body.kind == "dynamic":
        await segments.refresh(session, s)
    return _out(s)


@router.post("/preview")
async def preview_definition(body: PreviewIn, agent: Agent = Depends(current_agent),
                             session: AsyncSession = Depends(get_session)):
    """Vista previa sin guardar (el constructor la llama mientras se editan las reglas)."""
    try:
        return await segments.preview(session, body.definition, agent.organization_id)
    except segments.SegmentError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/{segment_id}", response_model=SegmentOut)
async def get_segment(segment_id: int, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    s = await _segment(session, segment_id, agent.organization_id)
    return _out(s, await _using(session, agent.organization_id, s.id))


@router.put("/{segment_id}", response_model=SegmentOut)
async def update_segment(segment_id: int, body: SegmentIn, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    s = await _segment(session, segment_id, agent.organization_id)
    if body.kind != s.kind:
        raise HTTPException(422, "No se puede cambiar el tipo de un segmento")
    await _validate(session, agent.organization_id, body)
    dup = await session.scalar(select(Segment.id).where(Segment.organization_id == s.organization_id,
                                                        Segment.name == body.name.strip(), Segment.id != s.id))
    if dup:
        raise HTTPException(409, "Ya existe un segmento con ese nombre")
    s.name, s.description, s.refresh_minutes = body.name.strip(), body.description, body.refresh_minutes
    if s.kind == "dynamic":
        s.definition = body.definition
    else:
        await _set_static_members(session, s, body.contact_ids)
    await session.commit()
    return _out(s, await _using(session, agent.organization_id, s.id))


@router.delete("/{segment_id}")
async def delete_segment(segment_id: int, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    s = await _segment(session, segment_id, agent.organization_id)
    used = [j for j in await _using(session, agent.organization_id, s.id) if j.status in ("active", "paused")]
    if used:
        raise HTTPException(409, "El segmento lo usa un journey activo: " + ", ".join(j.name for j in used))
    await session.delete(s)
    await session.commit()
    return {"ok": True}


@router.post("/{segment_id}/preview")
async def preview_segment(segment_id: int, agent: Agent = Depends(current_agent),
                          session: AsyncSession = Depends(get_session)):
    s = await _segment(session, segment_id, agent.organization_id)
    try:
        return await segments.preview(session, s, agent.organization_id)
    except segments.SegmentError as e:
        raise HTTPException(422, str(e)) from e


@router.post("/{segment_id}/refresh")
async def refresh_segment(segment_id: int, agent: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """Recalcula ya: devuelve cuántos entraron / salieron (y los inscribe en sus journeys «entra al segmento»)."""
    from app.journeys.scheduler import refresh_segment as do_refresh

    s = await _segment(session, segment_id, agent.organization_id)
    try:
        r = await do_refresh(session, s)
    except segments.SegmentError as e:
        raise HTTPException(422, str(e)) from e
    return {"entered": len(r["entered"]), "left": len(r["left"]), "count": r["count"], "enrolled": r["enrolled"],
            "entered_ids": r["entered"][:200], "left_ids": r["left"][:200]}


@router.get("/{segment_id}/export.csv")
async def export_segment(segment_id: int, agent: Agent = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    s = await _segment(session, segment_id, agent.organization_id)
    try:
        ids = await segments.segment_contact_ids(session, s)
    except segments.SegmentError as e:
        raise HTTPException(422, str(e)) from e
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "nombre", "whatsapp", "usuario", "correo", "etapa", "ultima_interaccion"])
    for i in range(0, len(ids), 1000):
        rows = (await session.scalars(select(Contact).where(Contact.id.in_(ids[i:i + 1000])).order_by(Contact.id))).all()
        for c in rows:
            w.writerow([c.id, c.name or "", c.wa_id or "", c.wa_username or "", c.email or "", c.stage,
                        c.last_interaction_at.isoformat() if c.last_interaction_at else ""])
    name = "".join(ch if ch.isalnum() else "_" for ch in s.name)[:60] or "segmento"
    return Response("﻿" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
