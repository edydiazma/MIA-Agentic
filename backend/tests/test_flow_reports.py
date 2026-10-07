"""Reportes → Análisis de flujos (reporting.daily_flow_*) y tarjeta de integraciones del Centro de Control."""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, FlowRunStep, IntegrationConnection
from tests.conftest import settle, text
from tests.test_flows import _new_flow, b, fake_buttons  # noqa: F401  (fixture autouse del módulo de flujos)


async def test_flow_funnel_reach_dropoff_and_choices(client):
    c = client
    fid = await _new_flow(c, "Embudo menú", "embudomenu", [
        b("f1", "send_text", {"text": "1) Precios 2) Horario"}),
        b("f2", "switch_reply", {"options": ["Precios", "Horario"], "timeout_min": 30},
          **{"Precios": [b("f3", "repeat", {"times": 2}, body=[b("f4", "change_var", {"name": "k", "delta": 1})])],
             "other": [b("f5", "send_text", {"text": "Opción no válida"})]}),
        b("f6", "send_text", {"text": "Gracias"}),
    ])
    # Tres clientes: uno elige Precios, otro responde algo que no es opción y el tercero no responde.
    for i, reply in enumerate(["Precios", "zzz", None]):
        phone = f"57145000000{i}"
        await c.post("/webhooks/whatsapp", json=text(phone, f"fr.{i}.1", "embudomenu"))
        await settle()
        if reply:
            await c.post("/webhooks/whatsapp", json=text(phone, f"fr.{i}.2", reply))
            await settle()

    async with SessionLocal() as s:  # el motor desnormaliza organización y flujo en cada paso
        steps = (await s.scalars(select(FlowRunStep).where(FlowRunStep.flow_id == fid))).all()
    assert steps and all(st.organization_id == 1 for st in steps)

    summary = (await c.get("/api/reports/flows")).json()
    row = next(f for f in summary["flows"] if f["flow_id"] == fid)
    assert (row["started"], row["succeeded"], row["waiting"]) == (3, 2, 1)
    assert row["completion_pct"] == 66.7

    r = await c.get(f"/api/reports/flows/{fid}")
    assert r.status_code == 200, r.text
    data = r.json()
    blocks = {x["block_id"]: x for x in data["blocks"]}
    assert [x["block_id"] for x in data["blocks"]] == ["f1", "f2", "f3", "f4", "f5", "f6"]  # orden de lectura
    assert blocks["f1"]["runs"] == 3 and blocks["f1"]["reach_pct"] == 100.0
    assert blocks["f2"]["stuck"] == 1 and blocks["f2"]["waits"] == 3
    assert {o["choice"]: o["runs"] for o in blocks["f2"]["choices"]} == {"Precios": 1, "other": 1}
    assert blocks["f4"]["executions"] == 2 and blocks["f4"]["runs"] == 1 and blocks["f4"]["lane"] == "body"
    assert blocks["f6"]["runs"] == 2
    assert data["top_dropoff"] == "f2"
    assert blocks["f1"]["label"] and not blocks["f1"]["removed"]

    assert (await c.get("/api/reports/flows/999999")).status_code == 404
    await c.post(f"/api/flows/{fid}/pause")


async def test_integrations_card_reads_phase2_connections(client):
    c = client
    async with SessionLocal() as s:
        org = await s.scalar(select(Agent.organization_id).where(Agent.email == "admin@test.com"))
        conn = await s.scalar(select(IntegrationConnection).where(
            IntegrationConnection.organization_id == org, IntegrationConnection.provider == "salesforce"))
        if not conn:
            s.add(IntegrationConnection(organization_id=org, provider="salesforce", status="connected",
                                        last_error="Token expirado"))
        else:
            conn.status, conn.last_error = "connected", "Token expirado"
        await s.commit()

    items = {i["key"]: i for i in (await c.get("/api/integrations")).json()}
    assert set(items) == {"hubspot", "salesforce", "google_ads", "meta_ads"}
    assert all(i["available"] and i["href"] for i in items.values())
    assert items["salesforce"]["connected"] and items["salesforce"]["last_error"] == "Token expirado"
    card = (await c.get("/api/control-center")).json()["cards"]["integrations"]
    assert card["total"] == 4 and card["connected"] >= 1 and card["failing"] >= 1
