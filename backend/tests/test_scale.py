"""Escala y producción: líder por advisory lock, latido, límites de uso, eventos entre réplicas por
LISTEN/NOTIFY (con derrame y aislamiento por empresa), presencia en el clúster, /health/ready y /metrics."""

import asyncio
import uuid

from sqlalchemy import text

from app.config import get_settings
from app.db import engine
from app.ops import heartbeat, leader, ratelimit
from app.realtime import Hub, hub


class FakeSocket:
    def __init__(self):
        self.received: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.received.append(payload)


async def _until(cond, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def test_only_one_leader_runs_each_loop_and_failover():
    name = f"test_loop_{uuid.uuid4().hex[:6]}"
    runs = {"a": 0, "b": 0}

    def job(key):
        async def fn():
            runs[key] += 1
            await asyncio.Event().wait()  # una tarea de fondo normal no termina
        return fn

    # Dos "réplicas": cada una con su propia conexión de locks
    sa, sb = leader.LockSession(), leader.LockSession()
    a = asyncio.create_task(leader.run_as_leader(name, job("a"), sa, lock_retry=0.1, check_every=0.1))
    await asyncio.sleep(0.3)
    b = asyncio.create_task(leader.run_as_leader(name, job("b"), sb, lock_retry=0.1, check_every=0.1))
    await asyncio.sleep(0.6)
    assert runs == {"a": 1, "b": 0}  # b no obtiene el lock mientras a viva

    a.cancel()  # la réplica líder se detiene: su conexión libera el lock
    await asyncio.gather(a, return_exceptions=True)
    assert await _until(lambda: runs["b"] == 1)
    b.cancel()
    await asyncio.gather(b, return_exceptions=True)
    await sa.close()
    await sb.close()


async def test_losing_the_lock_connection_stops_the_loop():
    name = f"test_drop_{uuid.uuid4().hex[:6]}"
    state = {"running": 0, "cancelled": 0}

    async def fn():
        state["running"] += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise

    sess = leader.LockSession()
    t = asyncio.create_task(leader.run_as_leader(name, fn, sess, lock_retry=0.1, check_every=0.1))
    assert await _until(lambda: state["running"] == 1)
    await sess.conn.close()  # la conexión del lock se cae (red, pooler)
    assert await _until(lambda: state["cancelled"] == 1)
    assert await _until(lambda: state["running"] == 2)  # reconecta, retoma el lock y vuelve a correr
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await sess.close()


async def test_failing_loop_is_restarted_and_recorded():
    name = f"test_fail_{uuid.uuid4().hex[:6]}"
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        raise RuntimeError("boom")

    sess = leader.LockSession()
    t = asyncio.create_task(leader.run_as_leader(name, flaky, sess, lock_retry=0.1, check_every=0.1))
    assert await _until(lambda: calls["n"] >= 2, timeout=6)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await sess.close()
    st = leader.STATUS[name]
    assert st["restarts"] >= 1 and "boom" in st["last_error"] and st["leader"] is False


async def test_heartbeat_upsert():
    await heartbeat.beat()
    await heartbeat.beat()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("select role, loops from public.worker_heartbeats where worker_id = :w"),
                                   {"w": heartbeat.WORKER_ID})).all()
    assert len(rows) == 1 and rows[0][0] == get_settings().role
    assert heartbeat.beat_age() is not None and heartbeat.beat_age() < 5


async def test_rate_limit_helper_and_429(client):
    bucket = f"test:{uuid.uuid4().hex}"
    assert [await ratelimit.allow(None, bucket, 2, 60) for _ in range(3)] == [True, True, False]

    ip = f"10.9.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    statuses = [(await client.get("/t/l/no-existe", headers={"X-Forwarded-For": ip},
                                  follow_redirects=False)).status_code for _ in range(61)]
    assert statuses[:60] == [404] * 60
    r = await client.get("/t/l/no-existe", headers={"X-Forwarded-For": ip}, follow_redirects=False)
    assert statuses[60] == 429 and r.status_code == 429 and r.headers["retry-after"] == "60"
    # Otra IP no está limitada
    other = await client.get("/t/l/no-existe", headers={"X-Forwarded-For": ip + "1"}, follow_redirects=False)
    assert other.status_code == 404


async def test_events_cross_replicas_with_spill_isolation_and_presence():
    a, b = Hub(mode="pg"), Hub(mode="pg")
    await b.start()
    try:
        assert await _until(lambda: b.listening)
        org7, org8 = FakeSocket(), FakeSocket()
        b.sockets[org7] = (7, 70)
        b.sockets[org8] = (8, 80)

        await a.broadcast("test.ping", {"n": 1}, 7)
        assert await _until(lambda: len(org7.received) == 1)
        assert '"test.ping"' in org7.received[0] and org8.received == []

        big = "x" * 20000  # no cabe en un NOTIFY: viaja por realtime_spill
        await a.broadcast("test.big", {"blob": big}, 7)
        assert await _until(lambda: len(org7.received) == 2)
        assert big in org7.received[1] and org8.received == []

        # b no se reenvía sus propios eventos (ya los entregó localmente)
        await b.broadcast("test.own", {}, 8)
        await asyncio.sleep(0.4)
        assert sum('"test.own"' in p for p in org8.received) == 1

        # Presencia: el asesor 99 conectado en a aparece en línea en b
        a.sockets[FakeSocket()] = (7, 99)
        await a.publish_presence()
        assert await _until(lambda: 99 in b.online_agent_ids(7))
        assert 99 not in b.online_agent_ids(8)
    finally:
        await b.stop()
    assert not b.listening


async def test_health_ready_and_metrics(client, monkeypatch):
    s = get_settings()
    r = await client.get("/health")
    assert r.json() == {"ok": True}

    await heartbeat.beat()
    await hub.start()
    try:
        assert await _until(lambda: hub.listening)
        r = await client.get("/health/ready")
        assert r.status_code == 200, r.text
        assert r.json()["checks"]["database"]["ok"] and r.json()["checks"]["realtime"]["ok"]
    finally:
        await hub.stop()
    r = await client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["realtime"]["ok"] is False

    m = await client.get("/metrics")
    assert m.status_code == 200
    assert 'wa_http_requests_total{method="GET",route="/health",status="200"}' in m.text
    assert "wa_http_request_duration_seconds_bucket" in m.text and "wa_ws_connections" in m.text
    assert m.headers.get("x-request-id")
    monkeypatch.setattr(s, "metrics_token", "secreto")
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer secreto"})).status_code == 200
