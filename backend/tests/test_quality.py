"""Calidad (QA) y coaching: revisión automática al cerrar, ponderación y críticos, coaching, revisión humana,
disputas, reporte, pruebas de agentes en arenero, plan y aislamiento entre empresas. LLM simulado."""

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from app import service
from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentTestRun,
    AIAgent,
    Channel,
    CoachingItem,
    Contact,
    Conversation,
    ConversationReview,
    Message,
    Organization,
    Plan,
    QAScorecard,
    utcnow,
)
from app.quality import agent_tests, hooks, reviews
from tests.conftest import WA, settle

FAKE: dict = {"scores": {}, "sentiment": "mixed", "judge": 90, "calls": 0, "na": set()}


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    async def complete_json(session, cx, system, user, schema, ctx, max_tokens=4000):
        FAKE["calls"] += 1
        if "criterion_key" in json.dumps(schema):
            keys = schema["properties"]["scores"]["items"]["properties"]["key"]["enum"]
            low = min(keys, key=lambda k: FAKE["scores"].get(k, 90))
            return {"scores": [{"key": k, "applies": k not in FAKE["na"], "score": FAKE["scores"].get(k, 90),
                                "evidence": f"cita {k}", "comment": f"comentario {k}"} for k in keys],
                    "sentiment": FAKE["sentiment"], "sentiment_score": -0.2, "customer_effort": 3,
                    "summary": "El cliente preguntó por precios.",
                    "coaching": [{"criterion_key": low, "title": "Verifica los datos", "suggestion": "Confirma precios",
                                  "example": "Déjame confirmarlo"},
                                 {"criterion_key": keys[0], "title": "Saluda", "suggestion": "Saluda por nombre",
                                  "example": "Hola Ana"}]}
        return {"score": FAKE["judge"], "reason": "evaluado"}

    monkeypatch.setattr(reviews, "complete_json", complete_json)
    monkeypatch.setattr(agent_tests, "complete_json", complete_json)
    FAKE.update(scores={}, sentiment="mixed", judge=90, calls=0, na=set())


async def _login(email: str, password: str) -> httpx.AsyncClient:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    r = await c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return c


async def _advisor(org: int, email: str) -> Agent:
    async with SessionLocal() as s:
        a = await s.scalar(select(Agent).where(Agent.email == email))
        if not a:
            a = Agent(organization_id=org, email=email, name=email.split("@")[0].title(), role="agent",
                      password_hash=hash_password("clave-asesor-1"))
            s.add(a)
            await s.commit()
        return a


async def _conversation(advisor_id: int, bot: bool = False) -> int:
    """Conversación de la empresa 1 con mensajes del cliente y del asesor (y opcionalmente del bot)."""
    async with SessionLocal() as s:
        ch = await s.scalar(select(Channel).where(Channel.organization_id == 1).order_by(Channel.id).limit(1))
        contact = Contact(organization_id=1, wa_id=f"5730{uuid.uuid4().int % 10**8:08d}", name="Ana Cliente")
        s.add(contact)
        await s.flush()
        conv = Conversation(organization_id=1, contact_id=contact.id, channel_id=ch.id, status="human",
                            assigned_agent_id=advisor_id)
        s.add(conv)
        await s.flush()
        t0 = utcnow() - timedelta(minutes=30)
        rows = [("in", "contact", None, "Hola, ¿cuánto vale el Onix? Mi número es 3001234567")]
        if bot:
            rows.append(("out", "bot", None, "¡Hola! Te paso con un asesor."))
        rows += [("out", "agent", advisor_id, "Hola Ana, cuesta 80 millones"), ("in", "contact", None, "Gracias"),
                 ("out", "agent", advisor_id, "Con gusto, ¿agendamos un test drive?")]
        for i, (d, st, aid, txt) in enumerate(rows):
            s.add(Message(organization_id=1, conversation_id=conv.id, direction=d, sender_type=st, sender_agent_id=aid,
                          text=txt, status="received" if d == "in" else "sent", created_at=t0 + timedelta(minutes=i)))
        await s.commit()
        return conv.id


async def _close(conv_id: int) -> None:
    async with SessionLocal() as s:
        conv = await s.get(Conversation, conv_id)
        await service.close(s, conv, None, actor="agent")


async def test_auto_review_on_close_weighting_coaching_and_idempotency(client):
    c = client
    cards = (await c.get("/api/quality/scorecards")).json()  # crea las rúbricas por defecto
    assert {x["name"] for x in cards} >= {"Atención de asesores", "Calidad del bot"}
    agent_card = next(x for x in cards if x["name"] == "Atención de asesores")

    advisor = await _advisor(1, "luisqa@test.com")
    FAKE["scores"] = {"informacion_correcta": 40}
    FAKE["na"] = {"objeciones"}  # no hubo objeciones: no cuenta en el total
    conv_id = await _conversation(advisor.id)
    await _close(conv_id)
    await settle(0.6)

    data = (await c.get(f"/api/quality/conversations/{conv_id}")).json()
    assert len(data["reviews"]) == 1
    r = data["reviews"][0]
    w = {cr["key"]: cr["weight"] for cr in agent_card["criteria"]}
    applied = {k: v for k, v in w.items() if k != "objeciones"}
    expected = round(sum(applied[k] * (40 if k == "informacion_correcta" else 90) for k in applied)
                     / sum(applied.values()), 2)
    assert r["total_score"] == expected and r["critical_failed"] is True
    assert r["agent_id"] == advisor.id and r["subject_type"] == "agent" and r["status"] == "done"
    assert data["qa_score"] == expected and data["sentiment"] == "neutral"  # mixed → neutral
    scores = {s["key"]: s for s in r["scores"]}
    assert scores["objeciones"]["applies"] is False and scores["informacion_correcta"]["evidence"]

    async with SessionLocal() as s:
        items = (await s.scalars(select(CoachingItem).where(CoachingItem.review_id == r["id"]))).all()
        assert [i.criterion_key for i in items] == ["informacion_correcta"]  # la de 90 no genera coaching
        conv = await s.get(Conversation, conv_id)
        # Idempotente: otra pasada (cierre repetido o bucle) no vuelve a llamar al LLM
        calls = FAKE["calls"]
        await reviews.review_conversation(s, conv)
        assert FAKE["calls"] == calls
        assert await s.scalar(select(func.count()).where(ConversationReview.conversation_id == conv_id)) == 1
    assert await hooks.review_pending() == 0  # ya revisada: el bucle no la toma

    # El LLM no recibe el número de teléfono
    async with SessionLocal() as s:
        txt = await reviews.transcript(s, await s.get(Conversation, conv_id))
    assert "3001234567" not in txt and "[número]" in txt and "Asesor (Luisqa)" in txt


async def test_human_review_dispute_resolve_coaching_and_report(client):
    c = client
    await c.get("/api/quality/scorecards")
    advisor = await _advisor(1, "carlaqa@test.com")
    other = await _advisor(1, "pedroqa@test.com")
    FAKE["scores"] = {"cierre": 30, "entendimiento": 60}
    conv_id = await _conversation(advisor.id)
    await _close(conv_id)
    await settle(0.6)
    review = (await c.get(f"/api/quality/conversations/{conv_id}")).json()["reviews"][0]

    me = await _login("carlaqa@test.com", "clave-asesor-1")
    them = await _login("pedroqa@test.com", "clave-asesor-1")
    try:
        mine = (await me.get("/api/quality/reviews")).json()
        assert [x["id"] for x in mine["items"]] == [review["id"]]  # el asesor solo ve las suyas
        assert (await them.get(f"/api/quality/reviews/{review['id']}")).status_code == 404
        assert (await them.post(f"/api/quality/reviews/{review['id']}/dispute", json={"note": "no"})).status_code == 403
        assert (await me.post(f"/api/quality/conversations/{conv_id}/review", json={})).status_code == 403

        r = await me.post(f"/api/quality/reviews/{review['id']}/dispute", json={"note": "Sí propuse el siguiente paso"})
        assert r.status_code == 200 and r.json()["status"] == "disputed"
        r = await c.post(f"/api/quality/reviews/{review['id']}/resolve",
                         json={"action": "adjust", "scores": {"cierre": {"score": 90}}, "note": "Tiene razón"})
        assert r.status_code == 200, r.text
        fixed = r.json()
        assert fixed["status"] == "done" and fixed["total_score"] > review["total_score"]
        assert "Tiene razón" in fixed["dispute_note"]

        # Coaching del asesor: sus sugerencias y su perfil (debilidad: entendimiento)
        items = (await me.get("/api/quality/coaching")).json()
        assert items and all(i["agent_id"] == advisor.id for i in items)
        r = await me.patch(f"/api/quality/coaching/{items[0]['id']}", json={"status": "done"})
        assert r.json()["status"] == "done" and r.json()["resolved_at"]
        assert (await them.patch(f"/api/quality/coaching/{items[0]['id']}", json={"status": "open"})).status_code == 403
        prof = (await me.get("/api/quality/coaching/profile")).json()
        assert prof["reviews"] == 1 and prof["weaknesses"][0]["key"] == "entendimiento"
        assert (await me.get("/api/quality/coaching/profile", params={"agent_id": other.id})).status_code == 403
        assert (await me.get("/api/reports/qa")).status_code == 403
    finally:
        await me.aclose()
        await them.aclose()

    # Revisión humana de un supervisor con la misma rúbrica
    card = next(x for x in (await c.get("/api/quality/scorecards")).json() if x["name"] == "Atención de asesores")
    r = await c.post(f"/api/quality/conversations/{conv_id}/human-review", json={
        "scorecard_id": card["id"], "scores": {"saludo_empatia": {"score": 100}, "informacion_correcta": {"score": 20}},
        "summary": "Revisado a mano"})
    assert r.status_code == 200, r.text
    assert r.json()["reviewer_type"] == "human" and r.json()["critical_failed"] is True
    assert (await c.post(f"/api/quality/conversations/{conv_id}/human-review",
                         json={"scorecard_id": card["id"], "scores": {}})).status_code == 422

    async with SessionLocal() as s:
        await s.execute(text("select reporting.refresh_range(1, current_date - 1, current_date + 1)"))
        await s.commit()
    rep = (await c.get("/api/reports/qa")).json()
    row = next(a for a in rep["agents"] if a["agent_id"] == advisor.id)
    assert row["reviews"] == 2 and row["critical_failed"] >= 1
    assert rep["totals"]["reviews"] >= 2 and len(rep["series"]) >= 1


async def test_agent_tests_sandbox_checks_and_run_on_change(client, monkeypatch):
    c = client
    async with SessionLocal() as s:
        bot = await s.scalar(select(AIAgent).where(AIAgent.organization_id == 1).order_by(AIAgent.id).limit(1))
    r = await c.post("/api/agent-tests/suites", json={"ai_agent_id": bot.id, "name": f"Básicas {uuid.uuid4().hex[:4]}",
                                                      "run_on_change": True, "min_pass_pct": 80})
    assert r.status_code == 200, r.text
    suite = r.json()
    cases = [
        ("Saluda", [{"role": "user", "text": "hola"}], {"must_include": ["AYUDO"]}),  # sin tildes ni mayúsculas
        ("Pide asesor", [{"role": "user", "text": "quiero un asesor"}], {"expect_handoff": True,
                                                                          "expect_tool": "transfer_to_human"}),
        ("No saluda", [{"role": "user", "text": "hola"}], {"must_not_include": ["hola"]}),
        ("Rúbrica", [{"role": "assistant", "text": "previo"}, {"role": "user", "text": "hola"}],
         {"rubric": "Pregunta el presupuesto del cliente"}),
    ]
    for name, turns, exp in cases:
        r = await c.post(f"/api/agent-tests/suites/{suite['id']}/cases", json={"name": name, "turns": turns,
                                                                               "expectations": exp})
        assert r.status_code == 200, r.text
    assert (await c.post(f"/api/agent-tests/suites/{suite['id']}/cases",
                         json={"name": "x", "turns": [{"role": "assistant", "text": "solo bot"}]})).status_code == 422

    FAKE["judge"] = 50
    sent_before = len(WA.sent)
    r = await c.post(f"/api/agent-tests/suites/{suite['id']}/run", params={"wait": True})
    assert r.status_code == 200, r.text
    run = r.json()
    assert (run["status"], run["total"], run["passed"], run["pass_pct"]) == ("failed", 4, 2, 50.0)
    detail = (await c.get(f"/api/agent-tests/runs/{run['id']}")).json()
    by_case = {x["case_name"]: x for x in detail["results"]}
    assert by_case["Saluda"]["passed"] and by_case["Pide asesor"]["passed"]
    assert not by_case["No saluda"]["passed"]
    assert not by_case["Rúbrica"]["passed"] and by_case["Rúbrica"]["judge_score"] == 50
    assert len(WA.sent) == sent_before  # arenero: no se envió nada por WhatsApp

    # Caso desde una conversación real (termina en el último mensaje del cliente)
    advisor = await _advisor(1, "luisqa@test.com")
    conv_id = await _conversation(advisor.id)
    r = await c.post(f"/api/agent-tests/suites/{suite['id']}/cases/from-conversation",
                     json={"conversation_id": conv_id, "expectations": {"must_include": ["ayudo"]}})
    assert r.status_code == 200, r.text
    assert r.json()["turns"][-1] == {"role": "user", "text": "Gracias"}

    # Guardar el agente corre las suites con run_on_change
    monkeypatch.setattr(hooks, "AGENT_CHANGE_DELAY_S", 0)
    r = await c.put(f"/api/bots/{bot.id}", json={"description": "probado"})
    assert r.status_code == 200, r.text
    await settle(1.0)
    async with SessionLocal() as s:
        last = (await s.scalars(select(AgentTestRun).where(AgentTestRun.suite_id == suite["id"])
                                .order_by(AgentTestRun.id.desc()).limit(1))).first()
    assert last.trigger == "on_change" and last.status in ("passed", "failed") and last.total == 5


async def test_plan_gate_and_org_isolation(client):
    c = client
    advisor = await _advisor(1, "luisqa@test.com")
    conv_id = await _conversation(advisor.id)
    await _close(conv_id)
    await settle(0.6)
    review_id = (await c.get(f"/api/quality/conversations/{conv_id}")).json()["reviews"][0]["id"]

    async def org_with(org_id: int, plan_key: str, email: str) -> None:
        # id fijo (como test_ops/test_reports): no consume la secuencia que usan los registros de test_saas
        async with SessionLocal() as s:
            if await s.get(Organization, org_id):
                return
            plan = await s.scalar(select(Plan).where(Plan.key == plan_key))
            org = Organization(id=org_id, name=f"QA {plan_key}", slug=f"qa-{plan_key}-{org_id}", plan_id=plan.id,
                               timezone="America/Bogota")
            s.add(org)
            await s.flush()
            s.add(Agent(organization_id=org.id, email=email, name="Admin QA", role="admin",
                        password_hash=hash_password("clave-admin-1")))
            await s.commit()

    await org_with(9101, "team", "teamqa@test.com")
    await org_with(9102, "professional", "proqa@test.com")
    team = await _login("teamqa@test.com", "clave-admin-1")
    pro = await _login("proqa@test.com", "clave-admin-1")
    try:
        r = await team.get("/api/quality/scorecards")
        assert r.status_code == 402 and "Calidad y coaching" in r.json()["detail"]
        assert (await team.get("/api/agent-tests/suites")).status_code == 402
        assert (await pro.get(f"/api/quality/reviews/{review_id}")).status_code == 404
        assert (await pro.get(f"/api/quality/conversations/{conv_id}")).status_code == 404
        assert (await pro.post(f"/api/quality/conversations/{conv_id}/review", json={})).status_code == 404
        cards = (await pro.get("/api/quality/scorecards")).json()
        org1_cards = {q["id"] for q in (await c.get("/api/quality/scorecards")).json()}
        assert cards and not {x["id"] for x in cards} & org1_cards  # rúbricas propias de la otra empresa
        assert (await pro.get("/api/quality/reviews")).json()["total"] == 0
    finally:
        await team.aclose()
        await pro.aclose()

    # Validación de rúbricas
    bad = {"name": "Mala", "criteria": [{"label": "A", "weight": 0}]}
    assert (await c.post("/api/quality/scorecards", json=bad)).status_code == 422
    good = {"name": f"Ventas {uuid.uuid4().hex[:4]}", "applies_to": "any", "sample_pct": 0,
            "criteria": [{"label": "Precio claro", "weight": 60, "critical": True}, {"label": "Cierre", "weight": 40}]}
    r = await c.post("/api/quality/scorecards", json=good)
    assert r.status_code == 200 and [x["key"] for x in r.json()["criteria"]] == ["precio_claro", "cierre"]
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).where(QAScorecard.organization_id == 1, QAScorecard.sample_pct == 0))
