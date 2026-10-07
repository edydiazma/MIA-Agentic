"""Supervisión (Atom: "Seguimiento" del supervisor), asignación masiva, transcripción y roles en grupos. §18.2

Todo respeta el alcance de app/scope.py: un supervisor solo ve, asigna y descarga conversaciones de los grupos que
supervisa; fuera de alcance responde 404.
"""

import csv
import io
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_agent, require_admin
from app.db import get_session, set_actor
from app.models import (
    Agent,
    AgentGroup,
    Channel,
    Conversation,
    ConversationTag,
    Group,
    Message,
    Organization,
    Tag,
    utcnow,
)
from app.scope import Scope, agent_allowed, conversation_clause, group_allowed, require_conversation, scope_for
from app.service import commit_and_broadcast, conversation_out, get_conversation, pick_agent, system_note
from app.settings_store import get_setting

router = APIRouter(prefix="/api", tags=["monitoring"])


async def _supervisor_scope(session: AsyncSession, agent: Agent) -> Scope:
    """Monitoreo: administradores, supervisores y roles propios con alcance de grupos o total."""
    scope = await scope_for(session, agent)
    if agent.role in ("admin", "supervisor"):
        return scope
    # Solo roles PROPIOS con alcance de grupos o total; el rol del sistema «Asesor» no monitorea
    from app.scope import _role_scope

    if await _role_scope(session, agent) in ("all", "groups"):
        return scope
    raise HTTPException(403, "Solo supervisores y administradores pueden usar el monitoreo")


def _wait_expr(now: datetime):
    """Minutos que el cliente lleva esperando: último mensaje del cliente sin respuesta posterior de un asesor."""
    waiting = and_(Conversation.status == "human", Conversation.last_inbound_at.is_not(None),
                   or_(Conversation.last_agent_message_at.is_(None),
                       Conversation.last_inbound_at > Conversation.last_agent_message_at))
    return waiting, func.extract("epoch", literal(now) - Conversation.last_inbound_at) / 60.0


@router.get("/monitoring/conversations")
async def monitoring_conversations(
    status: str | None = Query(default="open", pattern="^(bot|human|closed|open|unassigned|all)$"),
    group_id: int | None = None,
    agent_id: int | None = None,
    channel_id: int | None = None,
    waiting_min_gte: float | None = None,      # el cliente espera respuesta del asesor hace ≥ N min
    stuck_in_bot_min: float | None = None,     # en bot, sin respuesta del cliente ni transferencia hace ≥ N min
    unattended: bool = False,                  # transferida y sin primera respuesta de asesor
    sla_breached: bool = False,                # primera respuesta tardía o pendiente por encima del SLA
    tag: str | None = None,
    typification_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: str = Query(default="wait", pattern="^(wait|last_message|created)$"),
    limit: int = Query(default=200, le=1000),
    agent: Agent = Depends(current_agent),
    session: AsyncSession = Depends(get_session),
):
    scope = await _supervisor_scope(session, agent)
    org = agent.organization_id
    now = utcnow()
    waiting_cond, wait_minutes = _wait_expr(now)
    wait_col = case((waiting_cond, wait_minutes), else_=None)
    stmt = select(Conversation, wait_col.label("wait_min")).where(Conversation.organization_id == org)
    clause = conversation_clause(scope)
    if clause is not None:
        stmt = stmt.where(clause)
    if status == "open":
        stmt = stmt.where(Conversation.status != "closed")
    elif status == "unassigned":
        stmt = stmt.where(Conversation.status == "human", Conversation.assigned_agent_id.is_(None))
    elif status != "all":
        stmt = stmt.where(Conversation.status == status)
    if group_id is not None:
        stmt = stmt.where(Conversation.group_id == group_id)
    if agent_id is not None:
        stmt = stmt.where(Conversation.assigned_agent_id == agent_id)
    if channel_id is not None:
        stmt = stmt.where(Conversation.channel_id == channel_id)
    if waiting_min_gte is not None:
        stmt = stmt.where(waiting_cond, wait_minutes >= waiting_min_gte)
    if stuck_in_bot_min is not None:
        stmt = stmt.where(Conversation.status == "bot",
                          Conversation.last_message_at <= now - timedelta(minutes=stuck_in_bot_min))
    if unattended:
        stmt = stmt.where(Conversation.handoff_at.is_not(None), Conversation.first_response_at.is_(None),
                          Conversation.status != "closed")
    if sla_breached:
        sla = (await get_setting(session, "conversations", org))["sla_minutes"]
        limit_at = timedelta(minutes=sla)
        stmt = stmt.where(Conversation.handoff_at.is_not(None), or_(
            and_(Conversation.first_response_at.is_(None), Conversation.handoff_at <= now - limit_at),
            Conversation.first_response_at - Conversation.handoff_at > limit_at))
    if tag:
        stmt = stmt.where(exists().where(ConversationTag.conversation_id == Conversation.id,
                                         ConversationTag.tag_id == Tag.id, Tag.organization_id == org,
                                         Tag.name == tag.strip().lower()))
    if typification_id is not None:
        stmt = stmt.where(Conversation.typification_id == typification_id)
    if date_from or date_to:
        tz = ZoneInfo((await session.get(Organization, org)).timezone)
        if date_from:
            stmt = stmt.where(Conversation.created_at >= datetime.combine(date_from, time.min, tz))
        if date_to:
            stmt = stmt.where(Conversation.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tz))
    order = {"wait": [func.coalesce(wait_col, -1).desc(),
                      Conversation.last_message_at.desc()],
             "last_message": [Conversation.last_message_at.desc()],
             "created": [Conversation.created_at.desc()]}[sort]
    rows = (await session.execute(stmt.order_by(*order).limit(limit))).unique().all()
    items = []
    for conv, wait_min in rows:
        out = conversation_out(conv)
        out["wait_minutes"] = round(float(wait_min), 1) if wait_min is not None else None
        out["assignment_count"] = conv.assignment_count
        out["is_returning"] = conv.is_returning
        out["minutes_in_bot"] = (round((now - conv.last_message_at).total_seconds() / 60, 1)
                                 if conv.status == "bot" else None)
        items.append(out)
    return {"total": len(items), "items": items}


class BulkAssignIn(BaseModel):
    conversation_ids: list[int] = Field(min_length=1, max_length=500)
    agent_id: int | None = None
    group_id: int | None = None
    note: str | None = None


@router.post("/monitoring/assign")
async def bulk_assign(body: BulkAssignIn, agent: Agent = Depends(current_agent),
                      session: AsyncSession = Depends(get_session)):
    """Asigna varias conversaciones a un asesor o a un grupo (el grupo elige al asesor disponible con menos carga)."""
    scope = await _supervisor_scope(session, agent)
    org = agent.organization_id
    if body.agent_id is None and body.group_id is None:
        raise HTTPException(422, "Indica un asesor o un grupo")
    target = None
    if body.agent_id is not None:
        target = await session.get(Agent, body.agent_id)
        if not target or target.organization_id != org or not target.is_active:
            raise HTTPException(404, "Asesor no encontrado")
        if not agent_allowed(scope, target.id):
            raise HTTPException(403, "Ese asesor no pertenece a tus grupos")
    if body.group_id is not None:
        g = await session.get(Group, body.group_id)
        if not g or g.organization_id != org:
            raise HTTPException(404, "Grupo no encontrado")
        if not group_allowed(scope, g.id):
            raise HTTPException(403, "No supervisas ese grupo")
    done, skipped = [], []
    for cid in dict.fromkeys(body.conversation_ids):
        conv = await get_conversation(session, cid, org)
        try:
            await require_conversation(session, agent, conv)
        except HTTPException:
            skipped.append({"id": cid, "reason": "no encontrada"})
            continue
        if conv.status == "closed":
            skipped.append({"id": cid, "reason": "cerrada"})
            continue
        await set_actor(session, "agent", agent.id)
        if body.group_id is not None:
            conv.group_id = body.group_id
        new_agent = target.id if target else await pick_agent(session, org, body.group_id)
        conv.status, conv.assigned_agent_id = "human", new_agent
        if conv.handoff_at is None:
            conv.handoff_at = utcnow()
        who = target.name if target else (f"grupo {conv.group.name}" if conv.group else "grupo")
        await system_note(session, conv, f"Asignada por {agent.name} a {who}" + (f": {body.note}" if body.note else ""))
        await commit_and_broadcast(session, conv)
        done.append({"id": cid, "assigned_agent_id": conv.assigned_agent_id, "group_id": conv.group_id})
    return {"assigned": done, "skipped": skipped}


# --- Transcripción -------------------------------------------------------------------------------------------
SENDER = {"contact": "Cliente", "bot": "Bot", "agent": "Asesor", "campaign": "Campaña", "flow": "Flujo",
          "system": "Sistema"}


async def _transcript_lines(session: AsyncSession, conv: Conversation) -> tuple[str, list[tuple[str, str, str]]]:
    org = await session.get(Organization, conv.organization_id)
    tz = ZoneInfo(org.timezone)
    msgs = (await session.scalars(select(Message).where(Message.conversation_id == conv.id)
                                  .order_by(Message.created_at, Message.id))).all()
    agents = {a.id: a.name for a in (await session.scalars(
        select(Agent).where(Agent.organization_id == conv.organization_id))).all()}
    lines = []
    for m in msgs:
        who = SENDER.get(m.sender_type, m.sender_type)
        if m.sender_type == "agent" and m.sender_agent_id in agents:
            who = f"Asesor · {agents[m.sender_agent_id]}"
        body = m.text or m.transcript or ""
        if m.media_path:
            body = (body + " " if body else "") + f"[{m.type}: /api/media/{m.id}]"
        if not body:
            body = f"[{m.type}]"
        lines.append((m.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"), who, body))
    title = f"Conversación #{conv.id} · {conv.contact.name or conv.contact.wa_id or 'Cliente'}"
    return title, lines


def _pdf(title: str, lines: list[tuple[str, str, str]]) -> bytes:
    """PDF de texto sin dependencias (Helvetica, WinAnsi: tildes y ñ)."""
    import textwrap

    rows = [title, ""]
    for ts, who, body in lines:
        rows.extend(textwrap.wrap(f"{ts}  {who}: {body}", 100, subsequent_indent="    ") or [""])
    per_page = 58
    pages = [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]

    def esc(t: str) -> bytes:
        b = t.encode("cp1252", "replace")
        return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")

    objs: list[bytes] = []
    font_id = 3
    page_ids = []
    content_ids = []
    for i, _page in enumerate(pages):
        page_ids.append(4 + i * 2)
        content_ids.append(5 + i * 2)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [" + b" ".join(f"{p} 0 R".encode() for p in page_ids)
                + f"] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i, page in enumerate(pages):
        stream = b"BT /F1 9 Tf 40 800 Td 12 TL " + b" ".join(b"(" + esc(r) + b") '" for r in page) + b" ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R "
                    f">> >> /Contents {content_ids[i]} 0 R >>".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


@router.get("/conversations/{conv_id}/transcript")
async def transcript(conv_id: int, format: str = Query(default="txt", pattern="^(txt|csv|pdf|html)$"),
                     agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    conv = await require_conversation(session, agent, await get_conversation(session, conv_id, agent.organization_id))
    title, lines = await _transcript_lines(session, conv)
    name = f"conversacion_{conv.id}"
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["fecha", "remitente", "mensaje"])
        w.writerows(lines)
        return Response("﻿" + buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}.csv"})
    if format == "pdf":
        return Response(_pdf(title, lines), media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    if format == "html":
        from html import escape

        body = "".join(f"<tr><td>{escape(ts)}</td><td><b>{escape(who)}</b></td><td>{escape(msg)}</td></tr>"
                       for ts, who, msg in lines)
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title>"
                            f"<style>body{{font:13px system-ui;margin:24px}}td{{padding:4px 8px;vertical-align:top;"
                            f"border-bottom:1px solid #eee}}</style><h2>{escape(title)}</h2><table>{body}</table>"
                            f"<script>window.print()</script>")
    text = title + "\n\n" + "\n".join(f"[{ts}] {who}: {msg}" for ts, who, msg in lines) + "\n"
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={name}.txt"})


# --- Roles en los grupos ------------------------------------------------------------------------------------
class MemberRoleIn(BaseModel):
    agent_id: int
    role: str = Field(pattern="^(member|supervisor)$")


async def _group(session: AsyncSession, org: int, group_id: int) -> Group:
    g = await session.get(Group, group_id)
    if not g or g.organization_id != org:
        raise HTTPException(404, "Grupo no encontrado")
    return g


@router.get("/groups/{group_id}/members")
async def group_members(group_id: int, agent: Agent = Depends(current_agent),
                        session: AsyncSession = Depends(get_session)):
    await _group(session, agent.organization_id, group_id)
    rows = (await session.execute(select(Agent.id, Agent.name, Agent.email, Agent.role, AgentGroup.role)
                                  .join(AgentGroup, AgentGroup.agent_id == Agent.id)
                                  .where(AgentGroup.group_id == group_id).order_by(Agent.name))).all()
    return [{"agent_id": a, "name": n, "email": e, "user_role": ur, "role": r} for a, n, e, ur, r in rows]


@router.put("/groups/{group_id}/members")
async def set_member_role(group_id: int, body: MemberRoleIn, admin: Agent = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    """Agrega al usuario al grupo (si no estaba) con rol miembro o supervisor."""
    await _group(session, admin.organization_id, group_id)
    target = await session.get(Agent, body.agent_id)
    if not target or target.organization_id != admin.organization_id:
        raise HTTPException(404, "Usuario no encontrado")
    row = await session.get(AgentGroup, (body.agent_id, group_id))
    if row is None:
        session.add(AgentGroup(agent_id=body.agent_id, group_id=group_id, role=body.role))
    else:
        row.role = body.role
    await session.commit()
    return {"agent_id": body.agent_id, "group_id": group_id, "role": body.role}


@router.delete("/groups/{group_id}/members/{agent_id}")
async def remove_member(group_id: int, agent_id: int, admin: Agent = Depends(require_admin),
                        session: AsyncSession = Depends(get_session)):
    await _group(session, admin.organization_id, group_id)
    row = await session.get(AgentGroup, (agent_id, group_id))
    if row is not None:
        await session.delete(row)
        await session.commit()
    return {"ok": True}


@router.get("/monitoring/supervised-groups")
async def supervised_groups(agent: Agent = Depends(current_agent), session: AsyncSession = Depends(get_session)):
    """Grupos que el usuario puede supervisar (para los filtros del monitoreo)."""
    scope = await _supervisor_scope(session, agent)
    stmt = select(Group).where(Group.organization_id == agent.organization_id).order_by(Group.name)
    if not scope.unrestricted:
        stmt = stmt.where(Group.id.in_(sorted(scope.group_ids) or [0]))
    channels = {c.id: c.name for c in (await session.scalars(
        select(Channel).where(Channel.organization_id == agent.organization_id))).all()}
    return {"scope": scope.kind, "groups": [{"id": g.id, "name": g.name} for g in (await session.scalars(stmt)).all()],
            "channels": [{"id": k, "name": v} for k, v in channels.items()]}
