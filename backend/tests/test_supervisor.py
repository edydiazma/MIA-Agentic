"""Supervisión y KPIs (§18.2): alcance por rol, sub-estados, monitoreo, asignación masiva, transcripción, KPIs de
servicio, reporte Login, tiempo real y enlaces — en una empresa aislada."""

from datetime import timedelta

import httpx
from sqlalchemy import select, text, update

from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentGroup,
    AgentSession,
    AgentStatusEvent,
    Channel,
    Contact,
    Conversation,
    Group,
    Message,
    Organization,
    utcnow,
)

ORG = 9420
PWD = "secreto-420"
IDS: dict = {}


async def _seed() -> dict:
    if IDS:
        return IDS
    now = utcnow()
    async with SessionLocal() as s:
        s.add(Organization(id=ORG, name="Org supervisión", slug="org-supervision", timezone="America/Bogota",
                           onboarding_completed_at=now))
        await s.flush()
        ga, gb = Group(organization_id=ORG, name="Ventas A"), Group(organization_id=ORG, name="Taller B")
        s.add_all([ga, gb])
        users = {k: Agent(organization_id=ORG, email=f"{k}@sup420.co", name=n, role=r, password_hash=hash_password(PWD))
                 for k, n, r in (("admin", "Admin 420", "admin"), ("sup", "Sofía Supervisora", "supervisor"),
                                 ("a1", "Andrés Asesor", "agent"), ("b1", "Beatriz Asesora", "agent"),
                                 ("b2", "Bruno Asesor", "agent"))}
        s.add_all(users.values())
        ch = Channel(organization_id=ORG, name="WA 420", phone_number_id="PN9420", display_phone="+57 300 420 0000")
        s.add(ch)
        await s.flush()
        s.add_all([AgentGroup(agent_id=users["sup"].id, group_id=ga.id, role="supervisor"),
                   AgentGroup(agent_id=users["a1"].id, group_id=ga.id),
                   AgentGroup(agent_id=users["b1"].id, group_id=gb.id),
                   AgentGroup(agent_id=users["b2"].id, group_id=gb.id)])
        ks = [Contact(organization_id=ORG, wa_id=f"57301420000{i}", name=f"Cliente {i}") for i in range(1, 6)]
        s.add_all(ks)
        await s.flush()

        async def conv(contact, **kw):
            c = Conversation(organization_id=ORG, contact_id=contact.id, channel_id=ch.id, **kw)
            s.add(c)
            await s.flush()
            return c

        # c1: grupo A, atendida por a1, cerrada (espera 10 min, AHT 30 min)
        c1 = await conv(ks[0], group_id=ga.id, status="human")
        c2 = await conv(ks[1], group_id=ga.id, status="human")      # no atendida → abandono
        c3 = await conv(ks[2], status="bot")                         # solo bot, sin grupo
        c4 = await conv(ks[3], group_id=gb.id, status="human")       # grupo B, reasignada
        c5 = await conv(ks[0], group_id=ga.id, status="human")       # recurrente, en cola esperando
        c6 = await conv(ks[4], group_id=ga.id, status="bot")         # nueva, estancada en el bot
        s.add_all([
            Message(organization_id=ORG, conversation_id=c1.id, direction="in", sender_type="contact",
                    text="Hola, vi esto https://tucarro.com.co/auto/123 ¿sigue disponible?"),
            Message(organization_id=ORG, conversation_id=c1.id, direction="out", sender_type="agent",
                    sender_agent_id=users["a1"].id, text="Sí, aquí la ficha: https://concesionario.co/ficha."),
            Message(organization_id=ORG, conversation_id=c4.id, direction="in", sender_type="contact",
                    text="Mi cita https://agenda.taller.co/x"),
            Message(organization_id=ORG, conversation_id=c5.id, direction="in", sender_type="contact", text="¿Hola?"),
            Message(organization_id=ORG, conversation_id=c6.id, direction="in", sender_type="contact", text="Info"),
        ])
        await s.flush()
        # Asignaciones (los triggers cuentan asignaciones y fijan first_assigned_at)
        await s.execute(update(Conversation).where(Conversation.id == c1.id).values(assigned_agent_id=users["a1"].id))
        await s.execute(update(Conversation).where(Conversation.id == c4.id).values(assigned_agent_id=users["b1"].id))
        await s.execute(update(Conversation).where(Conversation.id == c4.id).values(assigned_agent_id=users["b2"].id))
        m = lambda n: now - timedelta(minutes=n)  # noqa: E731
        await s.execute(update(Conversation).where(Conversation.id == c1.id).values(
            handoff_at=m(50), first_assigned_at=m(50), first_response_at=m(40), last_agent_message_at=m(40),
            status="closed", closed_at=m(20)))
        await s.execute(update(Conversation).where(Conversation.id == c2.id).values(
            handoff_at=m(30), status="closed", closed_at=m(5)))
        await s.execute(update(Conversation).where(Conversation.id == c3.id).values(status="closed", closed_at=m(10)))
        await s.execute(update(Conversation).where(Conversation.id == c4.id).values(
            handoff_at=m(25), first_assigned_at=m(25), first_response_at=m(15), last_agent_message_at=m(15),
            last_inbound_at=m(16)))
        await s.execute(update(Conversation).where(Conversation.id == c5.id).values(
            handoff_at=m(12), last_inbound_at=m(12)))
        await s.execute(update(Conversation).where(Conversation.id == c6.id).values(last_message_at=m(40)))
        await s.execute(update(Contact).where(Contact.id == ks[3].id).values(owner_agent_id=users["a1"].id))
        # Estados: tramo que cruza la medianoche de antier → ayer; sesión de ayer
        tz_day = text("select (now() at time zone 'America/Bogota')::date")
        today = (await s.execute(tz_day)).scalar_one()
        s.add(AgentStatusEvent(organization_id=ORG, agent_id=users["a1"].id, status_key="available",
                               started_at=_local(today - timedelta(days=2), 23), ended_at=_local(today - timedelta(days=1), 1),
                               duration_s=7200))
        s.add(AgentStatusEvent(organization_id=ORG, agent_id=users["a1"].id, status_key="away",
                               started_at=_local(today - timedelta(days=1), 1), ended_at=_local(today - timedelta(days=1), 2),
                               duration_s=3600))
        s.add(AgentSession(organization_id=ORG, agent_id=users["a1"].id, started_at=_local(today - timedelta(days=1), 0),
                           last_seen_at=_local(today - timedelta(days=1), 2), ended_at=_local(today - timedelta(days=1), 2)))
        await s.commit()
        await s.execute(text("select reporting.refresh_range(:o, :a, :b)"),
                        {"o": ORG, "a": today - timedelta(days=3), "b": today})
        await s.commit()
        IDS.update(ga=ga.id, gb=gb.id, today=today, **{k: u.id for k, u in users.items()},
                   **{f"c{i}": c.id for i, c in enumerate((c1, c2, c3, c4, c5, c6), start=1)},
                   **{f"k{i}": k.id for i, k in enumerate(ks, start=1)})
    return IDS


def _local(day, hour):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo("America/Bogota"))


async def _login(who: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post("/api/auth/login", json={"email": f"{who}@sup420.co", "password": PWD})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


def _ids(rows) -> set[int]:
    return {r["id"] for r in rows}


async def test_scope_matrix_lists_detail_contacts_agents():
    ids = await _seed()
    admin, sup, a1 = await _login("admin"), await _login("sup"), await _login("a1")
    every = {ids[f"c{i}"] for i in range(1, 7)}
    group_a = {ids["c1"], ids["c2"], ids["c5"], ids["c6"]}
    assert _ids((await admin.get("/api/conversations", params={"limit": 500})).json()) == every
    assert _ids((await sup.get("/api/conversations", params={"limit": 500})).json()) == group_a
    assert _ids((await a1.get("/api/conversations", params={"limit": 500})).json()) == every  # asesor: sin cambios
    assert (await sup.get(f"/api/conversations/{ids['c4']}")).status_code == 404
    assert (await sup.get(f"/api/conversations/{ids['c4']}/messages")).status_code == 404
    assert (await sup.get(f"/api/conversations/{ids['c1']}")).status_code == 200
    assert (await a1.get(f"/api/conversations/{ids['c4']}")).status_code == 200

    contacts = {r["id"] for r in (await sup.get("/api/contacts", params={"limit": 500})).json()["items"]}
    # k1, k2, k5 por conversaciones del grupo A; k4 porque su dueño (a1) es miembro del grupo A
    assert contacts == {ids["k1"], ids["k2"], ids["k4"], ids["k5"]}
    assert (await sup.get(f"/api/contacts/{ids['k3']}")).status_code == 404
    assert len((await admin.get("/api/contacts", params={"limit": 500})).json()["items"]) == 5

    agents = {a["id"] for a in (await sup.get("/api/agents")).json()}
    assert agents == {ids["sup"], ids["a1"]}
    assert ids["b1"] in {a["id"] for a in (await admin.get("/api/agents")).json()}

    # Roles en el grupo: un admin cambia de miembro a supervisor y viceversa; editar los grupos del usuario no lo borra
    r = await admin.put(f"/api/groups/{ids['gb']}/members", json={"agent_id": ids["sup"], "role": "supervisor"})
    assert r.status_code == 200
    assert ids["c4"] in _ids((await sup.get("/api/conversations", params={"limit": 500})).json())
    await admin.put(f"/api/agents/{ids['sup']}", json={"group_ids": [ids["ga"], ids["gb"]]})
    members = {m["agent_id"]: m["role"] for m in (await admin.get(f"/api/groups/{ids['gb']}/members")).json()}
    assert members[ids["sup"]] == "supervisor"
    assert (await admin.delete(f"/api/groups/{ids['gb']}/members/{ids['sup']}")).status_code == 200
    assert (await sup.put(f"/api/groups/{ids['ga']}/members", json={"agent_id": ids["a1"], "role": "supervisor"})
            ).status_code == 403
    assert ids["c4"] not in _ids((await sup.get("/api/conversations", params={"limit": 500})).json())
    for c in (admin, sup, a1):
        await c.aclose()


async def test_substates_filters_and_counts():
    ids = await _seed()
    admin, sup = await _login("admin"), await _login("sup")
    q = lambda **p: admin.get("/api/conversations", params={"limit": 500, **p})  # noqa: E731
    assert _ids((await q(substate="returning")).json()) == {ids["c5"]}
    assert ids["c6"] in _ids((await q(substate="new")).json())
    assert _ids((await q(substate="reassigned")).json()) == {ids["c4"]}
    assert _ids((await q(substate="active")).json()) == {ids["c4"]}
    assert _ids((await q(conversation_id=ids["c3"])).json()) == {ids["c3"]}
    assert _ids((await q(agent="beatriz")).json()) == set()  # c4 pasó a Bruno
    assert _ids((await q(agent="bruno")).json()) == {ids["c4"]}
    assert _ids((await q(group="taller")).json()) == {ids["c4"]}
    assert _ids((await q(owner_agent_id=ids["a1"])).json()) == {ids["c4"]}
    today = ids["today"].isoformat()
    assert len((await q(date_from=today, date_to=today)).json()) == 6

    counts = (await sup.get("/api/conversations/counts")).json()
    assert counts == {"new": 1, "returning": 1, "reassigned": 0, "active": 0, "unassigned": 1, "mine": 0,
                      "closed_today": 2}
    all_counts = (await admin.get("/api/conversations/counts")).json()
    assert all_counts["reassigned"] == 1 and all_counts["closed_today"] == 3
    await admin.aclose()
    await sup.aclose()


async def test_monitoring_filters_bulk_assign_and_transcript():
    ids = await _seed()
    sup, a1 = await _login("sup"), await _login("a1")
    assert (await a1.get("/api/monitoring/conversations")).status_code == 403
    m = lambda **p: sup.get("/api/monitoring/conversations", params=p)  # noqa: E731
    open_ = (await m()).json()["items"]
    assert _ids(open_) == {ids["c5"], ids["c6"]}
    assert open_[0]["id"] == ids["c5"] and 11 <= open_[0]["wait_minutes"] <= 13  # ordenado por espera
    assert _ids((await m(waiting_min_gte=10)).json()["items"]) == {ids["c5"]}
    assert _ids((await m(waiting_min_gte=30)).json()["items"]) == set()
    assert _ids((await m(stuck_in_bot_min=30)).json()["items"]) == {ids["c6"]}
    assert _ids((await m(unattended=True)).json()["items"]) == {ids["c5"]}
    assert _ids((await m(sla_breached=True)).json()["items"]) == {ids["c5"]}
    assert _ids((await m(status="all", sla_breached=True)).json()["items"]) == {ids["c1"], ids["c2"], ids["c5"]}
    groups = (await sup.get("/api/monitoring/supervised-groups")).json()
    assert groups["scope"] == "groups" and [g["id"] for g in groups["groups"]] == [ids["ga"]]

    r = await sup.post("/api/monitoring/assign", json={"conversation_ids": [ids["c5"], ids["c4"]], "agent_id": ids["a1"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body["assigned"]] == [ids["c5"]] and body["skipped"][0]["id"] == ids["c4"]
    assert (await sup.post("/api/monitoring/assign", json={"conversation_ids": [ids["c6"]], "agent_id": ids["b1"]})
            ).status_code == 403
    assert (await sup.post("/api/monitoring/assign", json={"conversation_ids": [ids["c6"]], "group_id": ids["gb"]})
            ).status_code == 403
    r = await sup.post("/api/monitoring/assign", json={"conversation_ids": [ids["c6"]], "group_id": ids["ga"]})
    assert r.json()["assigned"][0]["group_id"] == ids["ga"]
    async with SessionLocal() as s:
        c5 = await s.get(Conversation, ids["c5"])
        assert c5.assigned_agent_id == ids["a1"] and c5.status == "human" and c5.assignment_count == 1

    # Deja los datos como estaban (las demás pruebas cuentan sobre el mismo escenario)
    async with SessionLocal() as s:
        await s.execute(update(Conversation).where(Conversation.id == ids["c5"]).values(
            assigned_agent_id=None, assignment_count=0, first_assigned_at=None, assigned_at=None))
        await s.execute(update(Conversation).where(Conversation.id == ids["c6"]).values(
            status="bot", assigned_agent_id=None, assignment_count=0, first_assigned_at=None, assigned_at=None,
            handoff_at=None))
        await s.commit()

    t = await sup.get(f"/api/conversations/{ids['c1']}/transcript")
    assert t.status_code == 200 and "Cliente: Hola, vi esto" in t.text and "Asesor · Andrés Asesor" in t.text
    assert (await sup.get(f"/api/conversations/{ids['c1']}/transcript", params={"format": "csv"})).text \
        .lstrip("﻿").startswith("fecha,remitente,mensaje")
    pdf = await sup.get(f"/api/conversations/{ids['c1']}/transcript", params={"format": "pdf"})
    assert pdf.content.startswith(b"%PDF-1.4") and pdf.content.rstrip().endswith(b"%%EOF")
    assert "<table>" in (await sup.get(f"/api/conversations/{ids['c1']}/transcript", params={"format": "html"})).text
    assert (await sup.get(f"/api/conversations/{ids['c4']}/transcript")).status_code == 404
    await sup.aclose()
    await a1.aclose()


async def test_service_kpis_login_realtime_and_links():
    ids = await _seed()
    admin, sup = await _login("admin"), await _login("sup")
    rep = (await admin.get("/api/reports/service")).json()["totals"]
    assert (rep["cases"], rep["handoffs"], rep["attended"], rep["not_attended"], rep["abandoned"], rep["bot_only"],
            rep["closed"], rep["reassigned"], rep["returning_cases"], rep["unique_contacts"]) == \
           (6, 4, 2, 1, 1, 1, 3, 1, 1, 5)
    assert rep["aht_s"] == 1800 and rep["asa_s"] == 600
    assert rep["attention_rate_pct"] == 50.0 and rep["abandonment_rate_pct"] == 25.0
    assert rep["bot_containment_pct"] == 33.3
    sup_rep = (await sup.get("/api/reports/service")).json()
    assert sup_rep["totals"]["cases"] == 4 and {g["name"] for g in sup_rep["by_group"]} == {"Ventas A"}
    by_agent = {a["name"]: a for a in (await admin.get("/api/reports/service")).json()["by_agent"]}
    assert by_agent["Andrés Asesor"]["aht_s"] == 1800 and by_agent["Bruno Asesor"]["asa_s"] == 600
    empty = (await admin.get("/api/reports/service", params={"agent_id": 999999})).json()["totals"]
    assert empty["aht_s"] is None and empty["attention_rate_pct"] is None and empty["cases"] == 0

    start = (ids["today"] - timedelta(days=3)).isoformat()
    login = (await admin.get("/api/reports/login", params={"start": start})).json()
    a1 = next(a for a in login["agents"] if a["agent_id"] == ids["a1"])
    # Al iniciar sesión se abre un tramo "available" (operaciones): se toleran unos segundos de esta prueba
    assert 7200 <= a1["by_status"]["available"] < 7200 + 300 and a1["by_status"]["away"] == 3600
    assert a1["worked_s"] == a1["by_status"]["available"]  # "Ausente" no cuenta como tiempo laborado
    assert a1["total_s"] == a1["by_status"]["available"] + 3600
    assert a1["sessions"][0]["sessions"] == 1
    assert {st["key"] for st in login["statuses"]} >= {"available", "busy", "away", "offline"}
    async with SessionLocal() as s:
        days = (await s.execute(text("""select day, seconds from reporting.daily_agent_status
                                        where organization_id = :o and agent_id = :a and status_key = 'available'
                                        order by day"""), {"o": ORG, "a": ids["a1"]})).all()
    assert [int(sec) for _d, sec in days][:2] == [3600, 3600]  # el tramo de 2 h se parte en la medianoche
    sup_login = (await sup.get("/api/reports/login", params={"start": start})).json()
    assert {a["agent_id"] for a in sup_login["agents"]} == {ids["sup"], ids["a1"]}

    rt = (await admin.get("/api/reports/realtime")).json()
    assert rt["today"]["incoming"] == 6 and rt["today"]["closed"] == 3 and rt["today"]["handoffs"] == 4
    assert rt["today"]["abandoned"] == 1 and rt["today"]["attended"] == 2
    ga = next(g for g in rt["groups"] if g["group_id"] == ids["ga"])
    assert ga["agents"] == 2 and ga["queue"] >= 0
    assert all("status" in a for a in rt["agents"])
    sup_rt = (await sup.get("/api/reports/realtime")).json()
    assert [g["group_id"] for g in sup_rt["groups"]] == [ids["ga"]]
    assert {a["id"] for a in sup_rt["agents"]} == {ids["sup"], ids["a1"]}

    links = (await admin.get("/api/reports/links")).json()
    domains = {d["domain"]: d for d in links["domains"]}
    assert domains["tucarro.com.co"]["client"] == 1 and domains["concesionario.co"]["agent"] == 1
    assert any(u["url"] == "https://concesionario.co/ficha" for u in links["urls"])  # sin el punto final
    sup_links = {d["domain"] for d in (await sup.get("/api/reports/links")).json()["domains"]}
    assert "agenda.taller.co" not in sup_links and "tucarro.com.co" in sup_links

    csv_text = (await sup.get("/api/reports/conversations.csv")).text
    assert str(ids["c4"]) not in {line.split(",")[0] for line in csv_text.splitlines()[1:]}
    async with SessionLocal() as s:
        assert await s.scalar(select(Agent.id).where(Agent.id == ids["sup"]))
    await admin.aclose()
    await sup.aclose()
