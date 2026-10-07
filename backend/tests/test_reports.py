"""Reportes sobre los rollups (reporting.*) con datos creados por ORM + triggers, en una organización aislada."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.auth import hash_password
from app.db import SessionLocal, set_actor
from app.main import app
from app.models import (
    Agent,
    AICall,
    AIConnection,
    AIConnectionHealth,
    Channel,
    Contact,
    Conversation,
    ConversationTag,
    Message,
    Organization,
    Tag,
    Typification,
    utcnow,
)

ORG = 9


async def _seed() -> dict:
    async with SessionLocal() as s:
        if await s.get(Organization, ORG):
            ids = {"luis": await s.scalar(select(Agent.id).where(Agent.email == "luis9@test.com"))}
            return ids
        s.add(Organization(id=ORG, name="Org reportes", slug="org-reportes", timezone="America/Bogota"))
        await s.flush()
        admin = Agent(organization_id=ORG, email="admin9@test.com", name="Admin 9", role="admin",
                      password_hash=hash_password("secret99"))
        luis = Agent(organization_id=ORG, email="luis9@test.com", name="Luis 9", role="agent")
        venta = Typification(organization_id=ORG, name="Venta", is_success=True, position=1)
        reclamo = Typification(organization_id=ORG, name="Reclamo", position=2)
        ch = Channel(organization_id=ORG, name="WA9", phone_number_id="PN9")
        vip = Tag(organization_id=ORG, name="vip")
        s.add_all([admin, luis, venta, reclamo, ch, vip])
        await s.flush()
        contacts = [Contact(organization_id=ORG, wa_id=f"57390000000{i}", name=f"C{i}") for i in range(3)]
        s.add_all(contacts)
        await s.flush()

        now = utcnow()
        t0 = now - timedelta(minutes=90)

        async def conv(contact, **kw):
            c = Conversation(organization_id=ORG, contact_id=contact.id, channel_id=ch.id, created_at=t0,
                             last_message_at=t0, **kw)
            s.add(c)
            await s.flush()
            return c

        def msg(c, minutes, direction, sender, **kw):
            s.add(Message(organization_id=ORG, conversation_id=c.id, direction=direction, sender_type=sender,
                          text="x", created_at=t0 + timedelta(minutes=minutes), **kw))

        # 1) Venta con asesor dentro del SLA (2 min)
        c1 = await conv(contacts[0])
        msg(c1, 1, "in", "contact")
        msg(c1, 2, "out", "bot", pricing_category="service", billable=True)
        await s.flush()
        await set_actor(s, "bot")
        c1.status, c1.handoff_reason, c1.handoff_at = "human", "pidió asesor", t0 + timedelta(minutes=3)
        await s.flush()
        await set_actor(s, "agent", luis.id)
        c1.assigned_agent_id = luis.id
        await s.flush()
        msg(c1, 5, "out", "agent", sender_agent_id=luis.id)
        await s.flush()
        s.add(ConversationTag(conversation_id=c1.id, tag_id=vip.id, source="ai", confidence=0.9))
        c1.status, c1.typification_id, c1.ai_typification_id = "closed", venta.id, venta.id
        await s.flush()

        # 2) Resuelta solo por el bot; la IA dijo Venta y el asesor cerró como Reclamo (desacuerdo)
        c2 = await conv(contacts[1])
        msg(c2, 1, "in", "contact")
        msg(c2, 2, "out", "bot")
        await s.flush()
        await set_actor(s, "agent", luis.id)
        c2.status, c2.typification_id, c2.ai_typification_id = "closed", reclamo.id, venta.id
        await s.flush()

        # 3) Desde anuncio, transferida; el asesor responde fuera del SLA (10 min), sigue abierta
        c3 = await conv(contacts[2], ad_source_type="ad", ad_source_id="AD9", ad_headline="Onix 0 km")
        msg(c3, 1, "in", "contact")
        await s.flush()
        await set_actor(s, "bot")
        c3.status, c3.handoff_at, c3.assigned_agent_id = "human", t0 + timedelta(minutes=2), luis.id
        await s.flush()
        msg(c3, 12, "out", "agent", sender_agent_id=luis.id)

        # IA: 5 llamadas a una conexión (1 error, 1 failover) y circuito abierto
        conn = AIConnection(organization_id=ORG, name="Claude 9", provider="anthropic", model="claude-opus-5-5",
                            input_cost_per_mtok=4, output_cost_per_mtok=20)
        s.add(conn)
        await s.flush()
        for lat, status, fb in ((100, "ok", None), (200, "ok", None), (900, "ok", 1), (50, "error", None),
                                (300, "ok", None)):
            s.add(AICall(organization_id=ORG, connection_id=conn.id, purpose="chat", status=status, latency_ms=lat,
                         input_tokens=1000, output_tokens=100, cost_usd=0.006, fallback_from_call_id=fb))
        s.add(AIConnectionHealth(connection_id=conn.id, state="open", consecutive_failures=5))
        await s.commit()
        return {"luis": luis.id}


@pytest.fixture
async def c9():
    ids = await _seed()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/auth/login", json={"email": "admin9@test.com", "password": "secret99"})
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        c.ids = ids
        yield c


async def test_general_report_from_rollups(c9):
    g = (await c9.get("/api/reports/general")).json()
    t = g["totals"]
    assert t["new_conversations"] == 3  # aislado: no ve conversaciones de otras organizaciones
    assert t["handoffs"] == 2 and t["closed"] == 2 and t["sales"] == 1
    assert t["bot_resolution_pct"] == 50.0
    assert t["sla_pct"] == 50.0  # 2 min dentro, 10 min fuera (SLA 5 min)
    assert t["first_response_median_min"] == 6.0  # mediana(2, 10)
    assert t["inbound_messages"] == 3 and t["agent_messages"] == 2 and t["ad_conversations"] == 1
    assert g["typifications"] == {"Venta": 1, "Reclamo": 1}
    assert g["tags"] == {"vip": 1}
    assert g["ai"]["compared"] == 2 and g["ai"]["agreement_pct"] == 50.0
    assert g["ai"]["disagreements"] == [["Venta → Reclamo", 1]]
    assert sum(d["inbound"] for d in g["series"]) == 3
    # Idempotente: volver a pedir el reporte recalcula y da lo mismo
    assert (await c9.get("/api/reports/general")).json()["totals"] == t


async def test_agents_ctwa_inbound_billing(c9):
    a = (await c9.get("/api/reports/agents")).json()
    luis = next(x for x in a["agents"] if x["id"] == c9.ids["luis"])
    assert luis["messages_sent"] == 2 and luis["conversations_closed"] == 1 and luis["sales"] == 1
    assert luis["sla_pct"] == 50.0 and luis["first_response_median_min"] == 6.0
    assert a["handoffs"] == 2 and a["handoffs_answered"] == 2

    ctwa = (await c9.get("/api/reports/ctwa")).json()
    assert ctwa["total"] == 1 and ctwa["ads"][0]["ad_id"] == "AD9" and ctwa["ads"][0]["handoffs"] == 1

    inbound = (await c9.get("/api/reports/inbound")).json()
    assert inbound["by_source"] == {"ads": 1, "organic": 2} and inbound["messages"] == 3
    assert sum(inbound["by_hour"]) == 3
    assert inbound["bots"]["top_handoff_reasons"][0][0] in ("pidió asesor", "Sin motivo")

    billing = (await c9.get("/api/reports/billing")).json()
    assert billing["billable_total"] == 1 and billing["categories"]["service"] == {"total": 1, "billable": 1}

    out = (await c9.get("/api/reports/outbound")).json()
    assert out["by_sender"] == {"bot": 2, "agent": 2}


async def test_ai_report_and_control_center(c9):
    ai = (await c9.get("/api/reports/ai")).json()
    row = ai["by_connection"][0]
    assert row["name"] == "Claude 9" and row["calls"] == 5 and row["errors"] == 1 and row["fallbacks"] == 1
    assert row["latency_p50_ms"] == 200 and row["latency_p95_ms"] > 700
    assert row["state"] == "open" and row["input_tokens"] == 5000
    assert ai["totals"]["error_pct"] == 20.0 and abs(ai["totals"]["cost_usd"] - 0.03) < 1e-9

    cc = (await c9.get("/api/control-center")).json()
    assert cc["cards"]["ai"] == {"connections": 1, "open": 1, "half_open": 0}
    assert cc["meta_summary"]["accounts_total"] == 1

    rt = (await c9.get("/api/reports/realtime")).json()
    assert rt["by_status"]["human"] == 1 and rt["waiting_unassigned"] == 0

    stages = (await c9.get("/api/reports/stages")).json()
    assert stages == {"lead": 3}

    csv_text = (await c9.get("/api/reports/conversations.csv")).text.lstrip("﻿")
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("id,telefono,nombre") and "tipificacion_ia" in lines[0]
    assert len(lines) == 4 and any("Venta,Venta,vip" in line for line in lines)
