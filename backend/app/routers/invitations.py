"""Invitaciones de usuarios por correo (rol y grupos) con enlace de un solo uso. Ver docs/data-model.md §13."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_token, hash_password, require_admin
from app.db import get_session
from app.fields import EMAIL_RE
from app.models import Agent, AgentGroup, AgentInvitation, Group, Organization, utcnow
from app.onboarding import invites
from app.plans import enforce_limit
from app.schemas import AgentOut

router = APIRouter(prefix="/api", tags=["invitations"])
ROLES = ("admin", "supervisor", "agent")


class InviteIn(BaseModel):
    email: str
    name: str | None = None
    role: str = "agent"
    group_ids: list[int] = Field(default_factory=list)


class InvitesIn(BaseModel):
    invites: list[InviteIn]


def _out(inv: AgentInvitation, link: str | None = None, email_sent: bool | None = None) -> dict:
    return {"id": inv.id, "email": inv.email, "name": inv.name, "role": inv.role, "group_ids": list(inv.group_ids or []),
            "status": invites.status(inv), "expires_at": inv.expires_at, "accepted_at": inv.accepted_at,
            "created_at": inv.created_at, "email_sent": inv.email_sent_at is not None if email_sent is None
            else email_sent, **({"link": link} if link else {})}


@router.post("/invitations")
async def create_invitations(body: InvitesIn, admin: Agent = Depends(require_admin),
                             session: AsyncSession = Depends(get_session)):
    org = admin.organization_id
    if not body.invites:
        raise HTTPException(422, "Agrega al menos una invitación")
    clean: list[InviteIn] = []
    seen: set[str] = set()
    for i in body.invites:
        email = i.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise HTTPException(422, f"Correo inválido: {i.email}")
        if i.role not in ROLES:
            raise HTTPException(422, f"Rol inválido: {i.role}")
        if email in seen:
            continue
        seen.add(email)
        if await session.scalar(select(Agent.id).where(Agent.organization_id == org,
                                                       func.lower(Agent.email) == email)):
            raise HTTPException(409, f"{email} ya es usuario de la empresa")
        clean.append(i.model_copy(update={"email": email}))
    groups = {g for g in (await session.scalars(select(Group.id).where(Group.organization_id == org))).all()}
    for i in clean:
        if set(i.group_ids) - groups:
            raise HTTPException(404, "Grupo no encontrado")
    # Cada invitación pendiente reserva un cupo de usuario del plan
    await enforce_limit(session, org, "users", needed=len(clean) + await invites.pending_count(session, org))

    out = []
    for i in clean:
        # Reenviar a un correo ya invitado: la invitación anterior queda revocada
        for old in (await session.scalars(select(AgentInvitation).where(
                AgentInvitation.organization_id == org, AgentInvitation.email == i.email,
                AgentInvitation.accepted_at.is_(None), AgentInvitation.revoked_at.is_(None)))).all():
            old.revoked_at = utcnow()
        await session.flush()
        token = invites.new_token()
        inv = AgentInvitation(organization_id=org, email=i.email, name=(i.name or "").strip() or None, role=i.role,
                              group_ids=i.group_ids, token_hash=invites.token_hash(token),
                              expires_at=utcnow() + invites.TTL, invited_by=admin.id)
        session.add(inv)
        await session.flush()
        subject, text = await invites.email_text(session, inv, token, admin.name)
        sent = await invites.send_email(inv.email, subject, text)
        if sent:
            inv.email_sent_at = utcnow()
        out.append(_out(inv, invites.link(token), sent))
    await session.commit()
    return out


@router.get("/invitations")
async def list_invitations(admin: Agent = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(AgentInvitation).where(
        AgentInvitation.organization_id == admin.organization_id).order_by(AgentInvitation.created_at.desc()))).all()
    return [_out(r) for r in rows]


@router.delete("/invitations/{invitation_id}")
async def revoke_invitation(invitation_id: int, admin: Agent = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    inv = await session.get(AgentInvitation, invitation_id)
    if not inv or inv.organization_id != admin.organization_id:
        raise HTTPException(404, "Invitación no encontrada")
    if inv.accepted_at:
        raise HTTPException(409, "La invitación ya fue aceptada")
    inv.revoked_at = inv.revoked_at or utcnow()
    await session.commit()
    return _out(inv)


async def _valid(session: AsyncSession, token: str) -> AgentInvitation:
    inv = await invites.by_token(session, token)
    if inv is None:
        raise HTTPException(404, "Invitación no encontrada")
    st = invites.status(inv)
    if st != "pending":
        raise HTTPException(410, {"accepted": "Esta invitación ya fue usada", "revoked": "La invitación fue anulada",
                                  "expired": "La invitación venció: pide una nueva"}[st])
    return inv


@router.get("/invitations/accept/{token}")
async def invitation_info(token: str, session: AsyncSession = Depends(get_session)):
    """Público: datos para la pantalla de aceptación."""
    inv = await _valid(session, token)
    org = await session.get(Organization, inv.organization_id)
    return {"organization": org.name, "email": inv.email, "name": inv.name, "role": inv.role,
            "expires_at": inv.expires_at}


class AcceptIn(BaseModel):
    token: str
    name: str
    password: str


@router.post("/invitations/accept")
async def accept_invitation(body: AcceptIn, session: AsyncSession = Depends(get_session)):
    """Público: crea el usuario con el rol y los grupos de la invitación y devuelve la sesión."""
    inv = await _valid(session, body.token)
    if len(body.password) < 8:
        raise HTTPException(422, "La contraseña debe tener al menos 8 caracteres")
    if not body.name.strip():
        raise HTTPException(422, "Escribe tu nombre")
    if await session.scalar(select(Agent.id).where(Agent.organization_id == inv.organization_id,
                                                   func.lower(Agent.email) == inv.email)):
        raise HTTPException(409, "Ya existe un usuario con ese correo en la empresa")
    agent = Agent(organization_id=inv.organization_id, email=inv.email, name=body.name.strip(), role=inv.role,
                  password_hash=hash_password(body.password))
    session.add(agent)
    await session.flush()
    valid_groups = set((await session.scalars(select(Group.id).where(
        Group.organization_id == inv.organization_id, Group.id.in_(inv.group_ids or [0])))).all())
    for gid in valid_groups:
        session.add(AgentGroup(agent_id=agent.id, group_id=gid))
    inv.accepted_at, inv.accepted_agent_id = utcnow(), agent.id
    await session.commit()
    org = await session.get(Organization, inv.organization_id)
    orgs = list((await session.scalars(select(Agent.organization_id).where(
        func.lower(Agent.email) == inv.email, Agent.is_active))).all())
    return {"access_token": create_token(agent, orgs), "token_type": "bearer",
            "agent": AgentOut.model_validate(agent).model_dump(),
            "organization": {"id": org.id, "name": org.name, "status": org.status}}
