"""Operación de contact center (§18.1): estados de asesor con bitácora, sesiones, enrutamiento por estado y por
estrategia de grupo, dueño del cliente, reglas de transferencia y canales, horarios por grupo y reglas de SLA."""

from datetime import datetime, timedelta

from sqlalchemy import select, update

from app import business_hours, routing, statuses
from app.automations import run_sla
from app.db import SessionLocal
from app.models import (
    Agent,
    AgentSession,
    AgentStatus,
    AgentStatusEvent,
    AutomationRun,
    BusinessHours,
    Channel,
    Contact,
    Conversation,
    Group,
    Message,
    Organization,
    utcnow,
)
from app.realtime import hub
from tests.conftest import WA, FakeChat, settle, text

PASSWORD = "Clave-Segura-2026!"


async def _agent(c, email: str, name: str, group_ids: list[int], role: str = "agent") -> dict:
    r = await c.post("/api/agents", json={"email": email, "name": name, "password": PASSWORD, "role": role,
                                          "group_ids": group_ids})
    assert r.status_code == 200, r.text
    return r.json()


async def _conv(contact_phone: str, group_id: int | None = None, status: str = "human",
                agent_id: int | None = None, channel_id: int | None = None) -> tuple[int, int]:
    async with SessionLocal() as s:
        ch = channel_id or await s.scalar(select(Channel.id).where(Channel.organization_id == 1).order_by(Channel.id))
        k = Contact(organization_id=1, wa_id=contact_phone, name=f"Cliente {contact_phone[-3:]}")
        s.add(k)
        await s.flush()
        conv = Conversation(organization_id=1, contact_id=k.id, channel_id=ch, group_id=group_id, status=status,
                            assigned_agent_id=agent_id, handoff_at=utcnow() if status == "human" else None)
        s.add(conv)
        await s.commit()
        return conv.id, k.id


async def test_statuses_intervals_sessions_and_board(client):
    c = client
    rows = (await c.get("/api/agent-statuses")).json()
    keys = {r["key"]: r for r in rows}
    assert {"available", "busy", "away", "offline"} <= keys.keys() and keys["available"]["is_default"]
    lunch = (await c.post("/api/agent-statuses", json={"name": "Almuerzo", "icon": "🍽️", "counts_as_working": False})).json()
    training = (await c.post("/api/agent-statuses", json={"name": "Capacitación"})).json()
    assert lunch["key"] == "almuerzo" and not lunch["receives_conversations"]

    me = (await c.get("/api/auth/me")).json()
    r = await c.put("/api/me/status", json={"status_id": lunch["id"]})
    assert r.status_code == 200 and r.json()["availability"] == "away"
    r = await c.put("/api/me/status", json={"status_id": training["id"]})
    assert r.json()["availability"] == "busy"  # cuenta como trabajo pero no recibe
    r = await c.put("/api/me/status", json={"status_id": keys["available"]["id"]})
    assert r.json()["availability"] == "available"
    async with SessionLocal() as s:
        evs = (await s.scalars(select(AgentStatusEvent).where(AgentStatusEvent.agent_id == me["id"])
                               .order_by(AgentStatusEvent.id))).all()
    assert [e.status_key for e in evs][-3:] == ["almuerzo", "capacitacion", "available"]
    assert all(e.ended_at is not None and e.duration_s is not None for e in evs[:-1]) and evs[-1].ended_at is None

    # No se borran los del sistema ni uno en uso
    assert (await c.delete(f"/api/agent-statuses/{keys['away']['id']}")).status_code == 422
    await c.put("/api/me/status", json={"status_id": lunch["id"]})
    assert (await c.delete(f"/api/agent-statuses/{lunch['id']}")).status_code == 409

    # Tablero
    board = (await c.get("/api/agents/status-board")).json()
    row = next(b for b in board if b["agent_id"] == me["id"])
    assert row["status"]["key"] == "almuerzo" and row["seconds_in_status"] is not None

    # Salida → desconectado y sesión cerrada; latido → reabre sesión y estado inicial
    assert (await c.post("/api/me/logout")).status_code == 200
    st = (await c.get("/api/me/status")).json()
    assert st["status"]["key"] == "offline" and st["availability"] == "away"
    async with SessionLocal() as s:
        assert not await s.scalar(select(AgentSession.id).where(AgentSession.agent_id == me["id"],
                                                                AgentSession.ended_at.is_(None)))
    await c.post("/api/me/heartbeat")
    st = (await c.get("/api/me/status")).json()
    assert st["status"]["key"] == "available"


async def test_routing_statuses_strategies_owner_and_fallback(client, monkeypatch):
    c = client
    g = (await c.post("/api/groups", json={"name": "Ruteo"})).json()
    a = await _agent(c, "rt.a@test.com", "Ana R", [g["id"]])
    b = await _agent(c, "rt.b@test.com", "Beto R", [g["id"]])
    d = await _agent(c, "rt.d@test.com", "Dora R", [g["id"]])
    online = {a["id"], b["id"], d["id"]}
    monkeypatch.setattr(hub, "online_agent_ids", lambda organization_id=None: set(online))
    async with SessionLocal() as s:
        away = await s.scalar(select(AgentStatus).where(AgentStatus.organization_id == 1, AgentStatus.key == "away"))
        await statuses.set_status(s, await s.get(Agent, b["id"]), away)  # Beto no recibe

        picks = [await routing.pick_agent(s, 1, g["id"]) for _ in range(2)]
        assert b["id"] not in picks
        # Round robin: alterna entre los disponibles
        await s.execute(update(Group).where(Group.id == g["id"]).values(routing="round_robin", last_assigned_agent_id=None))
        await s.commit()
        rr = [await routing.pick_agent(s, 1, g["id"]) for _ in range(3)]
        assert rr == [a["id"], d["id"], a["id"]]

        # Máximo de abiertas por asesor
        await s.execute(update(Group).where(Group.id == g["id"]).values(routing="least_loaded", max_open_per_agent=1))
        await s.commit()
    conv_a, _ = await _conv("573500000001", g["id"], agent_id=a["id"])
    async with SessionLocal() as s:
        assert await routing.pick_agent(s, 1, g["id"]) == d["id"]

    # Dueño del cliente con routing sticky_owner (aunque tenga más carga)
    conv2, contact2 = await _conv("573500000002", g["id"], agent_id=None)
    async with SessionLocal() as s:
        await s.execute(update(Group).where(Group.id == g["id"]).values(routing="sticky_owner", max_open_per_agent=None))
        await s.execute(update(Contact).where(Contact.id == contact2).values(owner_agent_id=a["id"]))
        await s.commit()
        assert await routing.pick_agent(s, 1, g["id"], contact2) == a["id"]

    # Manual: nunca asigna sola
    async with SessionLocal() as s:
        await s.execute(update(Group).where(Group.id == g["id"]).values(routing="manual"))
        await s.commit()
        assert await routing.pick_agent(s, 1, g["id"]) is None
        await s.execute(update(Group).where(Group.id == g["id"]).values(routing="least_loaded"))
        await s.commit()

    # Nadie conectado: cola, salvo el ajuste assign_when_none_available
    online.clear()
    async with SessionLocal() as s:
        assert await routing.pick_agent(s, 1, g["id"], business_open=True) is None
    await c.put("/api/settings/routing", json={"assign_when_none_available": True})
    async with SessionLocal() as s:
        assert await routing.pick_agent(s, 1, g["id"], business_open=True) in {a["id"], b["id"], d["id"]}
        assert await routing.pick_agent(s, 1, g["id"], business_open=False) is None
    await c.put("/api/settings/routing", json={"assign_when_none_available": False})

    # Primera asignación → dueño del cliente
    conv3, contact3 = await _conv("573500000003", g["id"], status="bot")
    online.update({d["id"]})
    async with SessionLocal() as s:
        from app.service import handoff

        conv = await s.get(Conversation, conv3)
        await handoff(s, conv, "prueba", g["id"], actor="automation")
        owner = await s.scalar(select(Contact.owner_agent_id).where(Contact.id == contact3))
    assert owner == d["id"]


async def test_transfer_rules_channels_and_deactivation(client, monkeypatch):
    c = client
    g1 = (await c.post("/api/groups", json={"name": "Origen T"})).json()
    g2 = (await c.post("/api/groups", json={"name": "Permitido T"})).json()
    g3 = (await c.post("/api/groups", json={"name": "Prohibido T"})).json()
    a = await _agent(c, "tr.a@test.com", "Tito", [g1["id"]])
    b = await _agent(c, "tr.b@test.com", "Bea", [g2["id"]])
    r = await c.put(f"/api/groups/{g1['id']}/settings", json={"routing": "least_loaded", "transfer_group_ids": [g2["id"]],
                                                              "supervisor_ids": []})
    assert r.status_code == 200 and r.json()["transfer_group_ids"] == [g2["id"]]
    assert (await c.put(f"/api/groups/{g1['id']}/settings", json={"routing": "nope"})).status_code == 422

    login = await c.post("/api/auth/login", json={"email": "tr.a@test.com", "password": PASSWORD})
    token = login.json().get("access_token")
    assert token, login.text
    conv_id, contact_id = await _conv("573510000001", g1["id"], agent_id=a["id"])
    monkeypatch.setattr(hub, "online_agent_ids", lambda organization_id=None: {b["id"]})
    hdr = {"Authorization": f"Bearer {token}"}
    bad = await c.post(f"/api/conversations/{conv_id}/transfer", json={"group_id": g3["id"]}, headers=hdr)
    assert bad.status_code == 422
    ok = await c.post(f"/api/conversations/{conv_id}/transfer", json={"group_id": g2["id"]}, headers=hdr)
    assert ok.status_code == 200, ok.text
    assert ok.json()["assigned_agent"]["id"] == b["id"]  # auto-asignación dentro del grupo destino

    # Grupo que no atiende el canal → cola general
    async with SessionLocal() as s:
        ch = Channel(organization_id=1, name="Otro número", phone_number_id="PN-OTRO-CC")
        s.add(ch)
        await s.commit()
        other_channel = ch.id
    await c.put(f"/api/groups/{g3['id']}/settings", json={"routing": "least_loaded", "channel_ids": [other_channel]})
    conv2, _ = await _conv("573510000002", None, status="bot")
    async with SessionLocal() as s:
        from app.service import handoff

        conv = await s.get(Conversation, conv2)
        await handoff(s, conv, "prueba canal", g3["id"], actor="automation")
        assert (await s.get(Conversation, conv2)).group_id is None
    # Limpieza: el canal extra cuenta para el límite del plan de la empresa de pruebas
    await c.put(f"/api/groups/{g3['id']}/settings", json={"routing": "least_loaded", "channel_ids": []})
    async with SessionLocal() as s:
        await s.delete(await s.get(Channel, other_channel))
        await s.commit()

    # Desactivar: sus conversaciones y clientes pasan a otro asesor
    conv3, contact3 = await _conv("573510000003", g1["id"], agent_id=a["id"])
    async with SessionLocal() as s:
        await s.execute(update(Contact).where(Contact.id == contact3).values(owner_agent_id=a["id"]))
        await s.commit()
    r = await c.post(f"/api/agents/{a['id']}/deactivate", json={"transfer_to": b["id"]})
    assert r.status_code == 200 and r.json()["reassigned"] >= 1 and r.json()["owners_moved"] >= 1
    async with SessionLocal() as s:
        assert (await s.get(Conversation, conv3)).assigned_agent_id == b["id"]
        assert (await s.get(Contact, contact3)).owner_agent_id == b["id"]
        assert not (await s.get(Agent, a["id"])).is_active


async def test_business_hours_ranges_holidays_pause_bot(client, monkeypatch):
    c = client
    tz = "America/Bogota"
    sched = {"mon": [{"from": "08:00", "to": "12:00"}, {"from": "14:00", "to": "18:00"}],
             "tue": [{"from": "08:00", "to": "18:00"}]}
    bad = await c.put("/api/business-hours/general", json={"timezone": tz, "schedule": {"mon": [
        {"from": "08:00", "to": "12:00"}, {"from": "11:00", "to": "13:00"}]}})
    assert bad.status_code == 422  # superpuestos
    r = await c.put("/api/business-hours/general", json={"timezone": tz, "schedule": sched,
                                                          "out_of_hours_message": "Estamos cerrados.\nVolvemos pronto.",
                                                          "assign_anyway": False, "pause_bot": False})
    assert r.status_code == 200, r.text
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tz)
    async with SessionLocal() as s:
        row = await business_hours.hours_for(s, 1, None)
        mon = datetime(2026, 10, 5, tzinfo=z)  # lunes
        assert business_hours.open_at(row, mon.replace(hour=9))
        assert not business_hours.open_at(row, mon.replace(hour=13))  # entre rangos
        assert business_hours.open_at(row, mon.replace(hour=15))
        assert not business_hours.open_at(row, mon.replace(hour=9) + timedelta(days=2))  # miércoles sin horario
        assert not business_hours.open_at(row, mon.replace(hour=9), {mon.date()})  # festivo
    h = await c.post("/api/business-hours/holidays", json={"day": "2026-12-25", "name": "Navidad"})
    assert h.status_code == 200
    assert (await c.post("/api/business-hours/holidays", json={"day": "2026-12-25", "name": "x"})).status_code == 409

    # Grupo con horario propio que hereda el general
    g = (await c.post("/api/groups", json={"name": "Horario G"})).json()
    r = await c.put(f"/api/business-hours/groups/{g['id']}", json={"inherit_general": True})
    assert r.status_code == 200 and r.json()["schedule"]["mon"] == sched["mon"]
    data = (await c.get("/api/business-hours")).json()
    assert any(x["group_id"] == g["id"] and x["uses_general"] for x in data["groups"])

    # Siempre cerrado + bot en pausa: el mensaje se guarda, no responde la IA y el aviso va una sola vez
    await c.put("/api/business-hours/general", json={"timezone": tz, "schedule": {}, "pause_bot": True,
                                                     "out_of_hours_message": "Fuera de horario: te escribimos mañana."})
    phone = "573520000001"
    asked = len(FakeChat.requests)
    await c.post("/webhooks/whatsapp", json=text(phone, "bh.1", "hola"))
    await settle()
    await c.post("/webhooks/whatsapp", json=text(phone, "bh.2", "¿hay alguien?"))
    await settle()
    assert len(FakeChat.requests) == asked
    assert [m for m in WA.sent if m[0] == phone] == [(phone, "Fuera de horario: te escribimos mañana.")]

    # Cerrado y sin "asignar igual": la transferencia queda en cola; con "asignar igual" se asigna
    a = await _agent(c, "bh.a@test.com", "Hora A", [g["id"]])
    monkeypatch.setattr(hub, "online_agent_ids", lambda organization_id=None: {a["id"]})
    await c.put(f"/api/business-hours/groups/{g['id']}", json={"timezone": tz, "schedule": {}, "assign_anyway": False})
    conv1, _ = await _conv("573520000002", None, status="bot")
    async with SessionLocal() as s:
        from app.service import handoff

        await handoff(s, await s.get(Conversation, conv1), "x", g["id"], actor="automation")
        assert (await s.get(Conversation, conv1)).assigned_agent_id is None
    await c.put(f"/api/business-hours/groups/{g['id']}", json={"timezone": tz, "schedule": {}, "assign_anyway": True})
    conv2, _ = await _conv("573520000003", None, status="bot")
    async with SessionLocal() as s:
        from app.service import handoff

        await handoff(s, await s.get(Conversation, conv2), "x", g["id"], actor="automation")
        assert (await s.get(Conversation, conv2)).assigned_agent_id == a["id"]
    # Restablecer: sin horario configurado = siempre abierto para el resto de pruebas
    async with SessionLocal() as s:
        for row in (await s.scalars(select(BusinessHours).where(BusinessHours.organization_id == 1))).all():
            await s.delete(row)
        await s.commit()


async def test_sla_rules_fire_once_per_cycle(client, monkeypatch):
    c = client
    g = (await c.post("/api/groups", json={"name": "SLA G"})).json()
    a = await _agent(c, "sla.a@test.com", "Sla A", [g["id"]])
    b = await _agent(c, "sla.b@test.com", "Sla B", [g["id"]])
    sup = await _agent(c, "sla.s@test.com", "Sup S", [g["id"]], role="supervisor")
    await c.put(f"/api/groups/{g['id']}/settings", json={"routing": "least_loaded", "supervisor_ids": [sup["id"]]})
    monkeypatch.setattr(hub, "online_agent_ids", lambda organization_id=None: {a["id"], b["id"]})
    assert (await c.post("/api/automations", json={"name": "SLA malo", "type": "sla_agent_no_reply",
                                                   "config": {"minutes": 5, "actions": []}})).status_code == 422
    r = await c.post("/api/automations", json={
        "name": "Asesor no responde", "type": "sla_agent_no_reply",
        "config": {"minutes": 5, "group_ids": [g["id"]], "actions": [
            {"type": "send_message", "text": "Ya te atendemos, gracias por esperar."},
            {"type": "notify_supervisor"}, {"type": "reassign_in_group"}]}})
    assert r.status_code == 200, r.text
    broadcasts = []
    real = hub.broadcast

    async def spy(event, data, org):
        broadcasts.append((event, data))
        await real(event, data, org)

    monkeypatch.setattr(hub, "broadcast", spy)
    conv_id, _ = await _conv("573530000001", g["id"], agent_id=a["id"])
    past = utcnow() - timedelta(minutes=10)
    async with SessionLocal() as s:
        await s.execute(update(Conversation).where(Conversation.id == conv_id).values(
            last_inbound_at=past, assigned_at=past, last_agent_message_at=None))
        await s.commit()
    assert await run_sla() >= 1
    assert await run_sla() == 0  # mismo ciclo: no repite
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv.assigned_agent_id == b["id"]
        runs = (await s.scalars(select(AutomationRun).where(AutomationRun.conversation_id == conv_id))).all()
        assert len(runs) == 1 and "reassign_in_group" in runs[0].action
        notes = (await s.scalars(select(Message.text).where(Message.conversation_id == conv_id,
                                                            Message.sender_type == "system"))).all()
    assert any("SLA «Asesor no responde»" in n for n in notes)
    assert any(e == "sla.breach" and sup["id"] in d["supervisors"] for e, d in broadcasts)
    assert ("573530000001", "Ya te atendemos, gracias por esperar.") in WA.sent
    # Nuevo mensaje del cliente → nuevo ciclo
    async with SessionLocal() as s:
        later = utcnow() - timedelta(minutes=6)
        await s.execute(update(Conversation).where(Conversation.id == conv_id).values(
            last_inbound_at=later, assigned_at=past, last_agent_message_at=None))
        await s.commit()
    assert await run_sla() >= 1

    # Cola sin asesor → tipificar y cerrar; cliente que no responde al bot → transferir
    await c.post("/api/automations", json={"name": "Sin asignar", "type": "sla_unassigned",
                                           "config": {"minutes": 3, "group_ids": [g["id"]],
                                                      "actions": [{"type": "close"}]}})
    conv_u, _ = await _conv("573530000002", g["id"], agent_id=None)
    async with SessionLocal() as s:
        await s.execute(update(Conversation).where(Conversation.id == conv_u).values(handoff_at=utcnow() - timedelta(minutes=4)))
        await s.commit()
    await c.post("/api/automations", json={"name": "Cliente callado", "type": "sla_client_no_reply",
                                           "config": {"minutes": 30, "actions": [{"type": "handoff", "group_id": g["id"]}]}})
    conv_b, _ = await _conv("573530000003", None, status="bot")
    async with SessionLocal() as s:
        await s.execute(update(Conversation).where(Conversation.id == conv_b).values(
            last_inbound_at=utcnow() - timedelta(minutes=50), last_message_at=utcnow() - timedelta(minutes=40)))
        await s.commit()
    await run_sla()
    async with SessionLocal() as s:
        assert (await s.get(Conversation, conv_u)).status == "closed"
        cb = await s.get(Conversation, conv_b)
        assert cb.status == "human" and cb.group_id == g["id"]
    for r in (await c.get("/api/automations")).json():
        if r["type"].startswith("sla_"):
            await c.delete(f"/api/automations/{r['id']}")


async def test_org_isolation(client):
    async with SessionLocal() as s:
        org2 = await s.scalar(select(Organization.id).where(Organization.slug == "otra-cc-9431"))
        if org2 is None:
            o = Organization(id=9431, name="Otra CC", slug="otra-cc-9431")
            s.add(o)
            await s.commit()
            org2 = o.id
        other = await s.scalar(select(AgentStatus.id).where(AgentStatus.organization_id == org2))
    assert other is not None  # el trigger creó los estados de la otra empresa
    assert (await client.put("/api/me/status", json={"status_id": other})).status_code == 404
    keys = [r["id"] for r in (await client.get("/api/agent-statuses")).json()]
    assert other not in keys
    async with SessionLocal() as s:
        assert await s.scalar(select(AgentStatus.organization_id).where(AgentStatus.id == other)) == org2
