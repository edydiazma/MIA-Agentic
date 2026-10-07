"""Aislamiento multiempresa del tiempo real y de los webhooks salientes."""

import json

import httpx
import pytest

from app import webhooks_out
from app.realtime import Hub


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def accept(self):
        return None

    async def send_text(self, payload: str):
        self.sent.append(json.loads(payload))


async def test_hub_only_reaches_same_organization():
    hub = Hub()
    a, b = FakeWS(), FakeWS()
    await hub.connect(a, agent_id=10, organization_id=1)
    await hub.connect(b, agent_id=20, organization_id=2)
    a.sent.clear(), b.sent.clear()

    await hub.broadcast("call.incoming", {"sdp": "secreto-org-1"}, 1)
    assert [e["event"] for e in a.sent] == ["call.incoming"]
    assert b.sent == []  # la otra empresa nunca recibe el SDP ni los datos

    await hub.broadcast("message.new", {"text": "sin empresa"}, None)  # falla cerrado
    assert len(a.sent) == 1 and b.sent == []
    assert hub.online_agent_ids(1) == {10} and hub.online_agent_ids(2) == {20}


async def test_outbound_webhooks_only_for_event_organization(monkeypatch):
    calls: list[str] = []

    class FakeHTTP:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content, headers):
            calls.append(url)
            return httpx.Response(200)

    async def targets():
        return [(1, 1, "https://org1.example/hook", "s1", []), (2, 2, "https://org2.example/hook", "s2", [])]

    async def record(*a, **k):
        return None

    monkeypatch.setattr(webhooks_out.httpx, "AsyncClient", FakeHTTP)
    monkeypatch.setattr(webhooks_out, "_targets", targets)
    monkeypatch.setattr(webhooks_out, "_record", record)
    await webhooks_out.deliver("message.new", {"x": 1}, organization_id=2)
    assert calls == ["https://org2.example/hook"]
    calls.clear()
    await webhooks_out.deliver("message.new", {"x": 1})  # sin empresa: no se entrega a nadie
    assert calls == []


pytestmark = pytest.mark.asyncio
