"""Arquitectura de escala (§19.2) y llamadas iniciadas por el asesor (§19.1): cola de trabajos, nodos de voz,
permiso y llamada saliente, token de Supabase Realtime y sesión de reportes."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from sqlalchemy import select, text

from app import jobs
from app.config import get_settings
from app.db import SessionLocal, engine, reports_engine
from app.models import (
    Alert,
    Call,
    CallPermission,
    Channel,
    Contact,
    Conversation,
    Job,
    KeyExtraction,
    utcnow,
)
from app.realtime import hub
from app.voice import nodes
from app.voice.meta import CallingClient
from tests.conftest import inbound, settle

calls_seen: list[str] = []


@jobs.job("test.ok", queue="testq")
async def _ok(payload: dict) -> dict:
    calls_seen.append(payload["n"])
    return {"doubled": payload["n"] * 2}


@jobs.job("test.fail", queue="testq", max_attempts=2)
async def _fail(payload: dict) -> dict:
    raise RuntimeError("se cayó la integración")


async def _job(job_id: int) -> Job:
    async with SessionLocal() as s:
        return await s.get(Job, job_id)


# --- Cola de trabajos -----------------------------------------------------------------------------
async def test_enqueue_dedupe_claim_retry_and_dead(client):
    async with SessionLocal() as s:
        a = await jobs.enqueue(s, "test.ok", {"n": 21}, organization_id=1, dedupe_key="ok:21")
        dup = await jobs.enqueue(s, "test.ok", {"n": 21}, organization_id=1, dedupe_key="ok:21")
        f = await jobs.enqueue(s, "test.fail", {}, organization_id=1)
        await s.commit()
    assert a and dup is None  # misma dedupe_key mientras está pendiente → no se duplica
    assert (await _job(a)).queue == "testq"  # la cola del manejador registrado

    # Dos workers toman a la vez: cada trabajo una sola vez (SKIP LOCKED)
    async with SessionLocal() as s:
        for n in range(6):
            await jobs.enqueue(s, "test.ok", {"n": n}, organization_id=1)
        await s.commit()
    w1 = await jobs._claim("testq", "w1", 4, 60)
    w2 = await jobs._claim("testq", "w2", 10, 60)
    ids1, ids2 = {r["id"] for r in w1}, {r["id"] for r in w2}
    assert ids1 and ids2 and not ids1 & ids2 and len(ids1 | ids2) == 8
    for row in w1:
        await jobs.execute(row, "w1", 60)
    for row in w2:
        await jobs.execute(row, "w2", 60)

    done = await _job(a)
    assert done.status == "succeeded" and done.result == {"doubled": 42} and done.finished_at
    # Ya terminado: la misma dedupe_key se puede volver a encolar
    async with SessionLocal() as s:
        assert await jobs.enqueue(s, "test.ok", {"n": 1}, organization_id=1, dedupe_key="ok:21")
        await s.commit()

    first = await _job(f)
    assert first.status == "queued" and first.attempts == 1 and "se cayó" in first.last_error
    assert first.run_at > utcnow() + timedelta(seconds=20)  # espera exponencial antes del reintento
    async with engine.begin() as conn:
        await conn.execute(text("update public.jobs set run_at = now() where id = :i"), {"i": f})
    await jobs.run_pending("testq")
    dead = await _job(f)
    assert dead.status == "dead" and dead.attempts == 2
    async with SessionLocal() as s:
        assert await s.scalar(select(Alert.id).where(Alert.ref == f"job:{f}"))

    # Lease vencido (worker caído) → vuelve a la cola
    async with SessionLocal() as s:
        stuck = await jobs.enqueue(s, "test.ok", {"n": 5}, organization_id=1)
        await s.commit()
    await jobs._claim("testq", "ghost", 50, 60)
    async with engine.begin() as conn:
        await conn.execute(text("update public.jobs set locked_until = now() - interval '1 minute' where id = :i"),
                           {"i": stuck})
        await conn.execute(text("select public.jobs_requeue_stuck()"))
    assert (await _job(stuck)).status == "queued"
    assert await jobs.run_pending("testq") >= 1 and (await _job(stuck)).status == "succeeded"

    # Panel de administración: conteos, reintento y cancelación
    overview = (await client.get("/api/ops/jobs")).json()
    testq = next(q for q in overview["queues"] if q["queue"] == "testq")
    assert testq["dead"] >= 1 and testq["succeeded"] >= 8
    assert any(x["id"] == f for x in overview["recent_failures"])
    assert (await client.post(f"/api/ops/jobs/{f}/retry")).json()["status"] == "queued"
    assert (await client.post(f"/api/ops/jobs/{f}/cancel")).json()["status"] == "cancelled"
    assert (await client.post(f"/api/ops/jobs/{a}/cancel")).status_code == 409


async def test_schedulers_enqueue_migrated_work(client):
    """Los bucles ahora programan: una extracción pendiente se encola una sola vez y el manejador corre igual."""
    from app.golden.hooks import _schedule_pending

    async with SessionLocal() as s:
        contact = await s.scalar(select(Contact).where(Contact.organization_id == 1).limit(1))
        if contact is None:
            contact = Contact(organization_id=1, wa_id="573009990001", name="Doc")
            s.add(contact)
            await s.flush()
        ext = KeyExtraction(organization_id=1, contact_id=contact.id, source_kind="document", status="done",
                            created_at=utcnow() - timedelta(minutes=5))
        s.add(ext)
        await s.commit()
        # pendiente de hace más de 30 s (las recientes las atiende on_inbound_media)
        await s.execute(text("update public.key_extractions set status = 'pending' where id = :i"), {"i": ext.id})
        await s.commit()
    assert await _schedule_pending() >= 1
    assert await _schedule_pending() == 0  # sigue en cola: no se duplica
    async with SessionLocal() as s:
        queued = await s.scalar(select(Job).where(Job.kind == "golden.extract", Job.dedupe_key == f"kx:{ext.id}"))
        assert queued.queue == "media" and queued.organization_id == 1

    async def fake_process(extraction_id):
        async with SessionLocal() as s:
            row = await s.get(KeyExtraction, extraction_id)
            row.status = "done"
            await s.commit()
            return row

    import app.golden.extract as extract_mod

    original = extract_mod.process_extraction
    extract_mod.process_extraction = fake_process
    try:
        await jobs.run_pending("media")
    finally:
        extract_mod.process_extraction = original
    assert (await _job(queued.id)).status == "succeeded"
    async with SessionLocal() as s:
        assert (await s.get(KeyExtraction, ext.id)).status == "done"


# --- Nodos de voz ---------------------------------------------------------------------------------
class FakeNode:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        FakeNode.sent.append((url, json, headers))
        if url.endswith("/bridge"):
            return httpx.Response(200, json={"sdp": "v=0 node-answer"})
        return httpx.Response(200, json={"accepted": True})


FakeNode.sent = []


async def _beat(worker_id: str, endpoint: str, load: int, capacity: int = 10, age_s: int = 0) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("""
            insert into public.worker_heartbeats (worker_id, role, hostname, capacity, active_load, endpoint,
                                                  last_beat_at)
            values (:w, 'voice', 'h', :c, :l, :e, now() - make_interval(secs => :a))
            on conflict (worker_id) do update set capacity = excluded.capacity, active_load = excluded.active_load,
              endpoint = excluded.endpoint, last_beat_at = excluded.last_beat_at"""),
            {"w": worker_id, "c": capacity, "l": load, "e": endpoint, "a": age_s})


async def _conversation(wa_id: str) -> Conversation:
    async with SessionLocal() as s:
        channel = await s.scalar(select(Channel).where(Channel.phone_number_id == "PNID"))
        contact = await s.scalar(select(Contact).where(Contact.organization_id == 1, Contact.wa_id == wa_id))
        if contact is None:
            contact = Contact(organization_id=1, wa_id=wa_id, name="Cliente voz")
            s.add(contact)
            await s.flush()
        conv = Conversation(organization_id=1, contact_id=contact.id, channel_id=channel.id, status="human")
        s.add(conv)
        await s.commit()
        return conv


async def test_voice_node_selection_forwarding_and_failover(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "voice_dispatch", "remote")
    monkeypatch.setattr(s, "voice_internal_token", "tok-voz")
    monkeypatch.setattr(nodes, "http_client", lambda: FakeNode())
    FakeNode.sent.clear()
    await _beat("voz-llena", "http://full:8000", load=10, capacity=10)
    await _beat("voz-vieja", "http://stale:8000", load=0, age_s=600)
    await _beat("voz-ocupada", "http://busy:8000", load=6)
    await _beat("voz-libre", "http://free:8000", load=1)
    async with SessionLocal() as session:
        node = await nodes.pick_node(session)
    assert node["worker_id"] == "voz-libre"  # sana, con capacidad y la menos cargada

    conv = await _conversation("573008880001")
    async with SessionLocal() as session:
        call = Call(organization_id=1, channel_id=conv.channel_id, contact_id=conv.contact_id,
                    conversation_id=conv.id, wa_call_id="wacid.node1", direction="inbound", status="ringing")
        session.add(call)
        await session.commit()
        await nodes.start(session, call, "v=0 offer")
        assert call.media_node_id == "voz-libre"
    url, body, headers = FakeNode.sent[-1]
    assert url == "http://free:8000/internal/voice/start" and body == {"call_id": call.id, "sdp_offer": "v=0 offer"}
    assert headers["X-Voice-Token"] == "tok-voz"

    async with SessionLocal() as session:
        call = await session.get(Call, call.id)
        call.status = "transferring"
        await session.commit()
        assert nodes.has_session(call)
        assert await nodes.bridge(session, call, "v=0 browser") == "v=0 node-answer"
    assert FakeNode.sent[-1][0] == f"http://free:8000/internal/voice/{call.id}/bridge"

    # El nodo deja de latir con la llamada en curso → fallida + alerta (el audio no se puede migrar)
    await _beat("voz-libre", "http://free:8000", load=1, age_s=600)
    assert await nodes.fail_orphaned_calls() >= 1
    async with SessionLocal() as session:
        dead = await session.get(Call, call.id)
        assert dead.status == "failed" and "nodo de voz" in dead.end_reason
        assert await session.scalar(select(Alert.id).where(Alert.ref == f"call:{call.id}"))

    # Sin nodos sanos → suena en los asesores (no se pierde la llamada)
    async with engine.begin() as conn:
        await conn.execute(text("delete from public.worker_heartbeats where role = 'voice'"))
    rang = []

    async def fake_ring(call_id, sdp, reason):
        rang.append((call_id, reason))

    import app.voice.agent_runtime as runtime

    monkeypatch.setattr(runtime, "ring_agents_instead", fake_ring)
    async with SessionLocal() as session:
        call2 = Call(organization_id=1, channel_id=conv.channel_id, contact_id=conv.contact_id,
                     conversation_id=conv.id, wa_call_id="wacid.node2", direction="inbound", status="ringing")
        session.add(call2)
        await session.commit()
        await nodes.start(session, call2, "v=0 offer")
    assert rang and rang[0][0] == call2.id and "nodos de voz" in rang[0][1]

    # Endpoint interno: exige el token compartido
    r = await client.post("/internal/voice/start", json={"call_id": 1}, headers={"X-Voice-Token": "otro"})
    assert r.status_code == 401


# --- Llamadas iniciadas por el asesor ---------------------------------------------------------------
async def test_call_permission_and_outbound_call(client, monkeypatch):
    sent: list[tuple] = []

    async def request_permission(self, to, body=None):
        sent.append(("permission", to, body))
        return "wamid.perm1"

    async def connect(self, to, sdp_offer, callback_data=None):
        sent.append(("connect", to, sdp_offer))
        return "wacid.out1"

    monkeypatch.setattr(CallingClient, "request_permission", request_permission)
    monkeypatch.setattr(CallingClient, "connect", connect)
    to_agent: list[tuple] = []

    async def send_to_agent(org, agent_id, event, data):
        to_agent.append((agent_id, event, data))

    monkeypatch.setattr(hub, "send_to_agent", send_to_agent)
    me = (await client.get("/api/auth/me")).json()
    async with SessionLocal() as s:
        ch = await s.scalar(select(Channel).where(Channel.phone_number_id == "PNID"))
        ch.calling_enabled = True
        await s.commit()
    wa = "573007770001"
    conv = await _conversation(wa)

    r = await client.post(f"/api/conversations/{conv.id}/call", json={"sdp": "v=0 agent-offer"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_permission"
    state = (await client.get(f"/api/conversations/{conv.id}/call-permission")).json()
    assert state["status"] == "none" and state["can_request"] and not state["can_call"]

    r = await client.post(f"/api/conversations/{conv.id}/call-permission", json={})
    assert r.status_code == 200 and r.json()["status"] == "requested" and sent[-1][:2] == ("permission", wa)
    again = await client.post(f"/api/conversations/{conv.id}/call-permission", json={})
    assert again.status_code == 429 and again.json()["detail"]["code"] == "rate_limited"  # 1 cada 24 h

    exp = int((datetime.now(UTC) + timedelta(days=7)).timestamp())
    reply = inbound(wa, "wamid.scaleout.perm.reply", {"type": "interactive", "interactive": {
        "type": "call_permission_reply", "call_permission_reply": {
            "response": "accept", "is_permanent": False, "expiration_timestamp": str(exp),
            "response_source": "user_action"}}})
    await client.post("/webhooks/whatsapp", json=reply)
    await settle()
    state = (await client.get(f"/api/conversations/{conv.id}/call-permission")).json()
    assert state["status"] == "granted" and state["can_call"] and not state["permanent"]
    async with SessionLocal() as s:
        perm = await s.scalar(select(CallPermission).where(CallPermission.contact_id == conv.contact_id))
        assert abs(perm.expires_at.timestamp() - exp) < 2

    r = await client.post(f"/api/conversations/{conv.id}/call", json={"sdp": "v=0 agent-offer"})
    assert r.status_code == 200, r.text
    call = r.json()
    assert call["direction"] == "outbound" and call["status"] == "ringing" and call["wa_call_id"] == "wacid.out1"
    assert sent[-1] == ("connect", wa, "v=0 agent-offer")
    busy = await client.post(f"/api/conversations/{conv.id}/call", json={"sdp": "v=0 again"})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "busy"

    def calls_hook(payload: dict) -> dict:
        return {"entry": [{"id": "WABA", "changes": [{"field": "calls", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PNID"}, **payload}}]}]}

    await client.post("/webhooks/whatsapp", json=calls_hook({"calls": [{
        "id": "wacid.out1", "to": wa, "event": "connect", "direction": "BUSINESS_INITIATED",
        "session": {"sdp_type": "answer", "sdp": "v=0 client-answer"}}]}))
    await settle()
    assert (me["id"], "call.answer", {"call_id": call["id"], "sdp": "v=0 client-answer"}) in to_agent
    await client.post("/webhooks/whatsapp", json=calls_hook({"statuses": [{
        "id": "wacid.out1", "type": "call", "status": "ACCEPTED", "timestamp": "1", "recipient_id": wa}]}))
    await settle()
    async with SessionLocal() as s:
        row = await s.get(Call, call["id"])
        assert row.status == "connected" and row.answered_at and row.initiated_by_agent_id == me["id"]


# --- Supabase Realtime y reportes -------------------------------------------------------------------
class FakeSupabase:
    posts: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        FakeSupabase.posts.append((url, json, headers))
        return httpx.Response(202, json={})


async def test_realtime_token_supabase_publish_and_reports_session(client, monkeypatch):
    assert (await client.get("/api/realtime/token")).json() == {"enabled": False}  # por defecto: WebSocket

    s = get_settings()
    for k, v in {"realtime_transport": "both", "supabase_url": "https://proj.supabase.co",
                 "supabase_publishable_key": "sb_publishable_x", "supabase_jwt_secret": "jwt-secret-for-tests-0123456789abcdef",
                 "supabase_secret_key": "sb_secret_x"}.items():
        monkeypatch.setattr(s, k, v)
    me = (await client.get("/api/auth/me")).json()
    data = (await client.get("/api/realtime/token")).json()
    assert data["enabled"] and data["topics"] == {"events": "org:1:events", "agent": f"org:1:agent:{me['id']}"}
    claims = jwt.decode(data["token"], "jwt-secret-for-tests-0123456789abcdef", algorithms=["HS256"], audience="authenticated")
    assert claims["role"] == "authenticated" and claims["app_metadata"] == {"org_id": "1"}
    again = jwt.decode((await client.get("/api/realtime/token")).json()["token"], "jwt-secret-for-tests-0123456789abcdef",
                       algorithms=["HS256"], audience="authenticated")
    assert again["sub"] == claims["sub"]  # sujeto estable por asesor (lo reconocen las políticas de Realtime)

    import app.realtime as realtime_mod

    FakeSupabase.posts.clear()
    monkeypatch.setattr(realtime_mod, "supabase_http", lambda: FakeSupabase())
    monkeypatch.setattr(hub, "transport", "both")
    await hub.broadcast("conversation.updated", {"id": 7, "status": "human"}, 1)
    await hub.send_to_agent(1, me["id"], "notification.new", {"title": "Hola"})
    await settle(0.2)
    topics = {(p[1]["messages"][0]["topic"], p[1]["messages"][0]["event"]) for p in FakeSupabase.posts}
    assert ("org:1:events", "conversation.updated") in topics
    assert (f"org:1:agent:{me['id']}", "notification.new") in topics
    msg = next(p for p in FakeSupabase.posts if p[1]["messages"][0]["event"] == "conversation.updated")
    assert msg[1]["messages"][0]["payload"] == {"event": "conversation.updated", "data": {"id": 7, "status": "human"}}
    assert msg[1]["messages"][0]["private"] is True and msg[2]["apikey"] == "sb_secret_x"
    assert json.dumps(msg[1])  # serializable

    # Presencia por latido del panel (sin WebSocket)
    await client.post("/api/me/heartbeat")
    await hub.refresh_session_presence()
    assert me["id"] in hub.online_agent_ids(1)

    # Reportes: sin réplica configurada usan la principal; el refresco va siempre a la principal
    assert reports_engine is engine
    assert (await client.get("/api/reports/general")).status_code == 200


@pytest.fixture(autouse=True)
def _reset_hub_sessions():
    yield
    hub.session_pairs = set()
