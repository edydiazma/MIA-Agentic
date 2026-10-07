"""Segmentos y journeys de marketing (§21.1): reglas → SQL, vista previa, recálculo, CSV, campañas por segmento;
validación del grafo, publicación, motor (envío idempotente, esperas, ramas, A/B, horas de silencio, frecuencia,
consentimiento, opt-out, metas), entradas (segmento, evento, fecha, API), ganchos de estados / respuestas, clics y
reporte. Cada prueba crea sus propios clientes, segmentos y journeys (independientes del orden)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text as sql

from app.db import SessionLocal
from app.journeys import engine, scheduler, schema
from app.models import (
    Contact,
    Deal,
    Journey,
    JourneyEnrollment,
    JourneyEvent,
    Message,
    utcnow,
)
from app.routers.journeys import z_test
from tests.conftest import WA, eventually, settle, text, unique_name, unique_phone


# --- Ayudas ---------------------------------------------------------------------------------------------------------
async def _contact(name: str | None = None, **kw) -> Contact:
    if kw.get("blocked"):
        kw.setdefault("blocked_at", utcnow())
    if kw.get("marketing_opt_out"):
        kw.setdefault("opt_out_at", utcnow())
    async with SessionLocal() as s:
        c = Contact(organization_id=1, wa_id=unique_phone("5731"), name=name or unique_name("Cliente J"), **kw)
        s.add(c)
        await s.commit()
        return c


async def _tag(contact_id: int, tag: str) -> None:
    from app.service import tag_by_name

    async with SessionLocal() as s:
        t = await tag_by_name(s, 1, tag)
        await s.execute(sql("insert into public.contact_tags (contact_id, tag_id, source) values (:c, :t, 'agent') "
                            "on conflict do nothing"), {"c": contact_id, "t": t.id})
        await s.commit()


def _defn(*steps, start=None) -> dict:
    return {"start": start or steps[0]["id"], "steps": list(steps)}


async def _journey(c, definition: dict, entry: dict | None = None, settings: dict | None = None,
                   publish: bool = True) -> dict:
    r = await c.post("/api/journeys", json={"name": unique_name("Journey"), "entry": entry or {"type": "manual"},
                                            "settings": settings or {}, "definition": definition})
    assert r.status_code == 200, r.text
    j = r.json()
    if publish:
        r = await c.post(f"/api/journeys/{j['id']}/publish")
        assert r.status_code == 200, r.text
        return r.json()["journey"] | {"enrolled_on_publish": r.json()["enrolled"]}
    return j


async def _enrollment(journey_id: int, contact_id: int) -> JourneyEnrollment | None:
    async with SessionLocal() as s:
        return (await s.scalars(select(JourneyEnrollment).where(
            JourneyEnrollment.journey_id == journey_id, JourneyEnrollment.contact_id == contact_id)
            .order_by(JourneyEnrollment.enrolled_at.desc()).limit(1))).first()


async def _events(journey_id: int, contact_id: int | None = None) -> list[tuple[str, str]]:
    async with SessionLocal() as s:
        q = select(JourneyEvent.step_id, JourneyEvent.kind).where(JourneyEvent.journey_id == journey_id)
        if contact_id:
            q = q.where(JourneyEvent.contact_id == contact_id)
        return [(r[0], r[1]) for r in (await s.execute(q.order_by(JourneyEvent.created_at, JourneyEvent.id))).all()]


async def _run(journey_id: int, contact_id: int, now: datetime | None = None) -> str | None:
    e = await _enrollment(journey_id, contact_id)
    return await engine.run_enrollment(e.id, e.enrolled_at, now)


def _templates_to(phone: str) -> list[str]:
    return [name for to, name, _ in WA.templates_sent if to == phone]


TPL = {"template_name": "promo", "language": "es", "params": ["{{nombre}}", "un descuento"]}


# --- Segmentos ------------------------------------------------------------------------------------------------------
async def test_segment_rules_preview_refresh_export_and_campaign(client):
    c = client
    marker = unique_name("Segmarca").replace(" ", "")
    a = await _contact(f"{marker} Ana")
    b = await _contact(f"{marker} Beto")
    z = await _contact(f"{marker} Zoe", blocked=True)  # bloqueados nunca entran
    await _tag(a.id, f"vip{marker}".lower())

    fields = (await c.get("/api/segments/fields")).json()
    keys = {f["key"]: f for f in fields["fields"]}
    assert {"contact.stage", "vehicle.insurance_due", "consent.marketing", "source.first_channel",
            "golden.has_document", "order.paid_at"} <= keys.keys()
    assert "within_days" in keys["vehicle.insurance_due"]["ops"] and keys["contact.stage"]["options"]

    rule = {"all": [{"field": "contact.name", "op": "contains", "value": marker}]}
    r = await c.post("/api/segments", json={"name": unique_name("Seg"), "definition": rule, "refresh_minutes": 5})
    assert r.status_code == 200, r.text
    seg = r.json()
    assert seg["member_count"] == 2
    prev = (await c.post(f"/api/segments/{seg['id']}/preview")).json()
    assert prev["count"] == 2 and {x["id"] for x in prev["sample"]} == {a.id, b.id} and z.id not in prev["sample"]

    # any / not / tag / in
    tagged = {"all": [{"field": "contact.name", "op": "contains", "value": marker},
                      {"not": {"field": "contact.tag", "op": "eq", "value": f"vip{marker}".lower()}}]}
    assert (await c.post("/api/segments/preview", json={"definition": tagged})).json()["count"] == 1
    either = {"any": [{"field": "contact.name", "op": "eq", "value": f"{marker} Ana"},
                      {"field": "contact.name", "op": "in", "value": [f"{marker} Beto"]}]}
    assert (await c.post("/api/segments/preview", json={"definition": either})).json()["count"] == 2

    # Validación: campo / operador / valor inválidos → 422 (nunca SQL arbitrario)
    for bad in ({"all": [{"field": "k.id; drop table contacts", "op": "eq", "value": 1}]},
                {"all": [{"field": "contact.stage", "op": "contains", "value": "x"}]},
                {"all": [{"field": "vehicle.insurance_due", "op": "within_days", "value": "x"}]},
                {"all": [{"field": "contact.name", "op": "between", "value": [1]}]}):
        assert (await c.post("/api/segments/preview", json={"definition": bad})).status_code == 422, bad
    inj = {"all": [{"field": "contact.name", "op": "eq", "value": "x' or '1'='1"}]}
    assert (await c.post("/api/segments/preview", json={"definition": inj})).json()["count"] == 0

    # Recalcular: diff de entradas / salidas
    async with SessionLocal() as s:
        (await s.get(Contact, b.id)).name = "Otro nombre"
        await s.commit()
    newc = await _contact(f"{marker} Nuevo")
    diff = (await c.post(f"/api/segments/{seg['id']}/refresh")).json()
    assert diff["entered_ids"] == [newc.id] and diff["left_ids"] == [b.id] and diff["count"] == 2

    csv_text = (await c.get(f"/api/segments/{seg['id']}/export.csv")).text
    assert f"{marker} Ana" in csv_text and "Otro nombre" not in csv_text

    # Estático
    st = (await c.post("/api/segments", json={"name": unique_name("Estático"), "kind": "static",
                                              "contact_ids": [a.id, b.id, 999999999]})).json()
    assert st["member_count"] == 2
    assert (await c.post(f"/api/segments/{st['id']}/preview")).json()["count"] == 2

    # Campaña con audiencia por segmento
    camp = await c.post("/api/campaigns", json={"name": unique_name("Camp seg"), "template_name": "promo",
                                                "template_language": "es", "params": ["a", "b"],
                                                "segment_id": seg["id"]})
    assert camp.status_code == 200, camp.text
    assert camp.json()["audience"]["segment_id"] == seg["id"] and camp.json()["total"] == 2

    # Duplicado y borrado
    assert (await c.post("/api/segments", json={"name": seg["name"], "definition": rule})).status_code == 409
    assert (await c.delete(f"/api/segments/{st['id']}")).status_code == 200


async def test_segment_scheduled_refresh_enrolls_segment_enter(client):
    c = client
    marker = unique_name("Entra").replace(" ", "")
    first = await _contact(f"{marker} Uno")
    seg = (await c.post("/api/segments", json={"name": unique_name("SegE"), "refresh_minutes": 5,
                                               "definition": {"all": [{"field": "contact.name", "op": "contains",
                                                                       "value": marker}]}})).json()
    exit_only = _defn({"id": "x", "type": "exit", "config": {"reason": "fin"}})
    enter = await _journey(c, exit_only, {"type": "segment_enter", "segment_id": seg["id"]})
    member = await _journey(c, exit_only, {"type": "segment_member", "segment_id": seg["id"]})
    assert enter["enrolled_on_publish"] == 0 and member["enrolled_on_publish"] == 1  # miembros actuales
    assert await _enrollment(enter["id"], first.id) is None

    second = await _contact(f"{marker} Dos")
    # Vencido según refresh_minutes → el ciclo lo recalcula e inscribe a los nuevos en ambos journeys
    await scheduler.refresh_due_segments(utcnow() + timedelta(minutes=6))
    assert (await _enrollment(enter["id"], second.id)) is not None
    assert (await _enrollment(member["id"], second.id)) is not None
    # No vencido: no se recalcula
    async with SessionLocal() as s:
        before = (await s.execute(sql("select last_computed_at from public.segments where id = :i"),
                                  {"i": seg["id"]})).scalar()
    await scheduler.refresh_due_segments(utcnow())
    async with SessionLocal() as s:
        after = (await s.execute(sql("select last_computed_at from public.segments where id = :i"),
                                 {"i": seg["id"]})).scalar()
    assert before == after
    # Un segmento en uso por un journey activo no se borra
    assert (await c.delete(f"/api/segments/{seg['id']}")).status_code == 409


# --- Validación ---------------------------------------------------------------------------------------------------
def test_definition_validation():
    ok = _defn({"id": "s1", "type": "send_template", "config": TPL, "next": "w"},
               {"id": "w", "type": "wait", "config": {"minutes": 1}, "next": "ab"},
               {"id": "ab", "type": "split", "config": {"variants": [{"key": "A", "weight": 50},
                                                                     {"key": "B", "weight": 50}]},
                "branches": {"A": "b", "B": None}},
               {"id": "b", "type": "branch", "config": {"condition": {"type": "replied", "within_hours": 24}},
                "branches": {"yes": None, "no": "x"}},
               {"id": "x", "type": "exit"})
    assert schema.validate_definition(ok) == []
    assert [s["id"] for s in schema.ordered_steps(ok)] == ["s1", "w", "ab", "b", "x"]

    def errs(**changes):
        d = {"start": ok["start"], "steps": [dict(s) for s in ok["steps"]]}
        for sid, patch in changes.items():
            next(s for s in d["steps"] if s["id"] == sid).update(patch)
        return " | ".join(schema.validate_definition(d))

    assert "ciclo" in errs(x={"next": "s1"})
    assert "al menos 1 minuto" in errs(w={"config": {"seconds": 30}})
    assert "sumar 100" in errs(ab={"config": {"variants": [{"key": "A", "weight": 60}, {"key": "B", "weight": 50}]}})
    assert "«yes» y «no»" in errs(b={"branches": {"yes": None}})
    assert "no existe" in errs(w={"next": "fantasma"})
    assert "falta «template_name»" in errs(s1={"config": {"language": "es"}})
    orphan = {"start": "s1", "steps": ok["steps"] + [{"id": "suelto", "type": "exit"}]}
    assert "nunca se alcanzan" in " ".join(schema.validate_definition(orphan))
    assert schema.validate_definition({"start": "a", "steps": []})  # vacío
    assert schema.validate_entry({"type": "date_field", "field": "vehicle.insurance_due"})  # falta la hora
    assert schema.validate_entry({"type": "event", "event": "deal_won"}) == []
    assert schema.validate_settings({"frequency_cap": {"per_day": 0}})


async def test_publish_blocks_errors_and_unknown_templates(client):
    c = client
    bad = _defn({"id": "w", "type": "wait", "config": {"minutes": 0}})
    j = await _journey(c, bad, publish=False)
    r = await c.post(f"/api/journeys/{j['id']}/publish")
    assert r.status_code == 422 and "1 minuto" in r.text
    detail = (await c.get(f"/api/journeys/{j['id']}")).json()
    assert detail["errors"] and detail["version"]["version"] == 1

    # Nueva versión válida pero con una plantilla que no existe
    v = (await c.post(f"/api/journeys/{j['id']}/versions", json={"definition": _defn(
        {"id": "s", "type": "send_template", "config": {"template_name": "noexiste", "language": "es"}})})).json()
    assert v["version"] == 2 and v["errors"] == []
    r = await c.post(f"/api/journeys/{j['id']}/publish")
    assert r.status_code == 422 and "noexiste" in r.text
    assert (await c.post("/api/journeys", json={"name": "x", "entry": {"type": "event"}})).status_code == 422
    meta = (await c.get("/api/journeys/meta")).json()
    assert "split" in meta["step_types"] and "order_paid" in meta["events"]


# --- Motor ----------------------------------------------------------------------------------------------------------
async def test_engine_send_wait_branch_hooks_and_idempotency(client):
    c = client
    contact = await _contact()
    tag = unique_name("respondio").replace(" ", "").lower()
    j = await _journey(c, _defn(
        {"id": "s1", "type": "send_template", "config": TPL, "next": "w"},
        {"id": "w", "type": "wait", "config": {"hours": 1}, "next": "b"},
        {"id": "b", "type": "branch", "config": {"condition": {"type": "replied", "within_hours": 24}},
         "branches": {"yes": "t", "no": "x"}},
        {"id": "t", "type": "add_tag", "config": {"tags": [tag]}},
        {"id": "x", "type": "exit", "config": {"reason": "sin_respuesta"}}))
    r = await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})
    assert r.json()["enrolled"] == 1
    # Ya está activo: no se inscribe dos veces
    assert (await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})).json()["enrolled"] == 0

    assert await _run(j["id"], contact.id) == "waiting"
    assert _templates_to(contact.wa_id) == ["promo"]
    async with SessionLocal() as s:
        msg = (await s.scalars(select(Message).where(Message.journey_id == j["id"]))).one()
        assert msg.sender_type == "journey" and msg.template_name == "promo"
        wamid = msg.wa_message_id
    # Reintento (worker caído, trabajo repetido): nunca reenvía
    e = await _enrollment(j["id"], contact.id)
    async with SessionLocal() as s:
        row = await engine.get_enrollment(s, e.id, e.enrolled_at)
        row.current_step, row.status, row.next_run_at = "s1", "active", utcnow()
        await s.commit()
    await _run(j["id"], contact.id)
    assert _templates_to(contact.wa_id) == ["promo"]
    assert (await _enrollment(j["id"], contact.id)).current_step == "w"

    # Estados de WhatsApp → eventos delivered / read
    def status(st):
        return {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PNID"},
            "statuses": [{"id": wamid, "status": st, "recipient_id": contact.wa_id}]}}]}]}

    await c.post("/webhooks/whatsapp", json=status("read"))
    assert await eventually(lambda: _events(j["id"], contact.id),
                            lambda ev: ("s1", "read") in ev and ("s1", "delivered") in ev)
    # El cliente responde → replied (una vez) y la rama «sí» al vencer la espera
    await c.post("/webhooks/whatsapp", json=text(contact.wa_id, f"jr.{contact.id}", "me interesa"))
    assert await eventually(lambda: _events(j["id"], contact.id), lambda ev: ("s1", "replied") in ev)
    await settle(0.2)
    status_now = await _run(j["id"], contact.id, utcnow() + timedelta(hours=2))
    assert status_now == "completed"
    async with SessionLocal() as s:
        tags = (await s.execute(sql("select t.name from public.contact_tags ct join public.tags t on t.id = ct.tag_id "
                                    "where ct.contact_id = :c"), {"c": contact.id})).scalars().all()
    assert tag in tags
    kinds = [k for _, k in await _events(j["id"], contact.id)]
    assert kinds.count("sent") == 1 and kinds.count("replied") == 1 and kinds[-1] == "completed"


async def test_branch_no_reply_times_out_and_split_is_stable(client):
    c = client
    contact = await _contact()
    j = await _journey(c, _defn(
        {"id": "s1", "type": "send_template", "config": TPL, "next": "b"},
        {"id": "b", "type": "branch", "config": {"condition": {"type": "replied", "within_hours": 24}},
         "branches": {"yes": None, "no": "ab"}},
        {"id": "ab", "type": "split", "config": {"variants": [{"key": "A", "weight": 50}, {"key": "B", "weight": 50}]},
         "branches": {"A": "xa", "B": "xb"}},
        {"id": "xa", "type": "exit", "config": {"reason": "A"}},
        {"id": "xb", "type": "exit", "config": {"reason": "B"}}))
    await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})
    assert await _run(j["id"], contact.id) == "waiting"  # esperando respuesta hasta 24 h
    assert await _run(j["id"], contact.id, utcnow() + timedelta(hours=25)) == "exited"
    e = await _enrollment(j["id"], contact.id)
    assert e.variant in ("A", "B") and e.exit_reason == e.variant
    assert e.variant == engine.variant_for(e.id, "ab", [{"key": "A", "weight": 50}, {"key": "B", "weight": 50}])

    # Estable y aproximadamente proporcional
    variants = [{"key": "A", "weight": 20}, {"key": "B", "weight": 80}]
    picks = [engine.variant_for(i, "ab", variants) for i in range(4000)]
    assert picks == [engine.variant_for(i, "ab", variants) for i in range(4000)]
    assert 0.15 < picks.count("A") / 4000 < 0.25

    rep = (await c.get(f"/api/journeys/{j['id']}/report")).json()
    assert rep["ab"][0]["step_id"] == "ab" and sum(v["enrolled"] for v in rep["ab"][0]["variants"]) == 1
    steps = {s["id"]: s for s in rep["steps"]}
    assert steps["s1"]["sent"] == 1 and rep["totals"]["exited"] == 1


def test_z_test_and_quiet_hours():
    res = z_test(1000, 100, 1000, 150)
    assert res["significant"] and 3.3 < res["z"] < 3.5
    assert not z_test(100, 10, 100, 11)["significant"]
    assert z_test(0, 0, 10, 1)["z"] is None

    tz = ZoneInfo("America/Bogota")
    quiet = {"from": "20:00", "to": "08:00"}
    night = datetime(2026, 10, 7, 22, 30, tzinfo=tz)
    assert engine.in_quiet_hours(night, quiet, tz) == datetime(2026, 10, 8, 8, 0, tzinfo=tz).astimezone(UTC)
    early = datetime(2026, 10, 7, 6, 0, tzinfo=tz)
    assert engine.in_quiet_hours(early, quiet, tz) == datetime(2026, 10, 7, 8, 0, tzinfo=tz).astimezone(UTC)
    assert engine.in_quiet_hours(datetime(2026, 10, 7, 12, 0, tzinfo=tz), quiet, tz) is None
    assert engine.in_quiet_hours(datetime(2026, 10, 7, 13, 0, tzinfo=tz), {"from": "12:00", "to": "14:00"}, tz)
    nxt = engine.next_time_of_day(datetime(2026, 10, 7, 11, 0, tzinfo=tz), "10:00", tz)
    assert nxt == datetime(2026, 10, 8, 10, 0, tzinfo=tz).astimezone(UTC)


async def test_quiet_hours_frequency_cap_consent_and_opt_out(client):
    c = client
    send = _defn({"id": "s1", "type": "send_template", "config": TPL})

    # Horas de silencio: el envío se corre al final de la ventana
    quiet_c = await _contact()
    jq = await _journey(c, send, settings={"quiet_hours": {"from": "00:00", "to": "23:59", "timezone": "America/Bogota"}})
    await c.post(f"/api/journeys/{jq['id']}/enroll", json={"contact_ids": [quiet_c.id]})
    assert await _run(jq["id"], quiet_c.id, utcnow() + timedelta(seconds=5)) == "waiting"
    assert _templates_to(quiet_c.wa_id) == []

    # Consentimiento de marketing requerido y ausente → se salta el envío
    no_consent = await _contact()
    jc = await _journey(c, send, settings={"require_consent": "marketing"})
    await c.post(f"/api/journeys/{jc['id']}/enroll", json={"contact_ids": [no_consent.id]})
    assert await _run(jc["id"], no_consent.id) == "completed"
    assert _templates_to(no_consent.wa_id) == [] and ("s1", "skipped") in await _events(jc["id"], no_consent.id)
    # Con consentimiento sí
    ok = await _contact()
    async with SessionLocal() as s:
        await s.execute(sql("insert into public.contact_consents (organization_id, contact_id, consent_type, granted, "
                            "source) values (1, :c, 'marketing', true, 'api')"), {"c": ok.id})
        await s.commit()
    await c.post(f"/api/journeys/{jc['id']}/enroll", json={"contact_ids": [ok.id]})
    await _run(jc["id"], ok.id)
    assert _templates_to(ok.wa_id) == ["promo"]

    # Límite de frecuencia: 1 por día, ya recibió uno (del journey anterior) → se salta
    jf = await _journey(c, send, settings={"frequency_cap": {"per_day": 1}})
    await c.post(f"/api/journeys/{jf['id']}/enroll", json={"contact_ids": [ok.id]})
    await _run(jf["id"], ok.id)
    assert _templates_to(ok.wa_id) == ["promo"]
    ev = await _events(jf["id"], ok.id)
    assert ("s1", "skipped") in ev

    # Opt-out → sale del journey; bloqueado → no se inscribe
    opted = await _contact(marketing_opt_out=True)
    jo = await _journey(c, send)
    await c.post(f"/api/journeys/{jo['id']}/enroll", json={"contact_ids": [opted.id]})
    assert await _run(jo["id"], opted.id) == "exited"
    assert (await _enrollment(jo["id"], opted.id)).exit_reason == "opt_out"
    blocked = await _contact(blocked=True)
    assert (await c.post(f"/api/journeys/{jo['id']}/enroll", json={"contact_ids": [blocked.id]})).json()["enrolled"] == 0


async def test_reentry_pause_resume_archive(client):
    c = client
    contact = await _contact()
    wait = _defn({"id": "w", "type": "wait", "config": {"minutes": 5}})
    j = await _journey(c, wait, settings={"reentry": "always"})
    await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})
    assert await _run(j["id"], contact.id) == "waiting"
    assert (await c.post(f"/api/journeys/{j['id']}/pause")).json()["status"] == "paused"
    # En pausa: no avanza aunque la espera haya vencido
    assert await _run(j["id"], contact.id, utcnow() + timedelta(minutes=10)) == "waiting"
    assert (await c.post(f"/api/journeys/{j['id']}/resume")).json()["status"] == "active"
    assert await _run(j["id"], contact.id, utcnow() + timedelta(minutes=10)) == "completed"
    # reentry=always: puede volver a entrar al terminar
    assert (await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})).json()["enrolled"] == 1
    assert (await c.post(f"/api/journeys/{j['id']}/archive")).json()["status"] == "archived"
    assert (await _enrollment(j["id"], contact.id)).exit_reason == "journey_archived"
    enr = (await c.get(f"/api/journeys/{j['id']}/enrollments")).json()
    assert len(enr) == 2 and {x["status"] for x in enr} == {"completed", "exited"}
    # reentry=never (por defecto)
    j2 = await _journey(c, _defn({"id": "x", "type": "exit"}))
    await c.post(f"/api/journeys/{j2['id']}/enroll", json={"contact_ids": [contact.id]})
    await _run(j2["id"], contact.id)
    assert (await c.post(f"/api/journeys/{j2['id']}/enroll", json={"contact_ids": [contact.id]})).json()["enrolled"] == 0


async def test_actions_field_branch_and_deal(client):
    c = client
    contact = await _contact(stage="lead")
    j = await _journey(c, _defn(
        {"id": "u", "type": "update_contact", "config": {"field": "stage", "value": "prospect"}, "next": "b"},
        {"id": "b", "type": "branch", "config": {"condition": {"type": "field", "rule": {
            "all": [{"field": "contact.stage", "op": "eq", "value": "prospect"}]}}}, "branches": {"yes": "d", "no": None}},
        {"id": "d", "type": "create_deal", "config": {"pipeline": "default", "name": "Oferta {{nombre}}"}, "next": "m"},
        {"id": "m", "type": "move_stage", "config": {"pipeline": "default", "stage": "qualified"}}))
    await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})
    assert await _run(j["id"], contact.id) == "completed"
    async with SessionLocal() as s:
        assert (await s.get(Contact, contact.id)).stage == "prospect"
        deal = (await s.scalars(select(Deal).where(Deal.contact_id == contact.id))).one()
        assert deal.stage == "qualified" and deal.origin == f"journey:{j['id']}"


# --- Entradas por evento, fecha y API; metas ------------------------------------------------------------------------
async def test_event_entry_and_goal(client):
    c = client
    contact = await _contact()
    wait = _defn({"id": "w", "type": "wait", "config": {"days": 3}})
    entry_j = await _journey(c, wait, {"type": "event", "event": "deal_lost", "filter": {"pipeline": "default"}})
    goal_j = await _journey(c, wait, settings={"goal": {"event": "deal_won"}})
    await c.post(f"/api/journeys/{goal_j['id']}/enroll", json={"contact_ids": [contact.id]})
    await _run(goal_j["id"], contact.id)

    await scheduler.poll_events(utcnow())  # fija los cursores (la primera vez no hay pasado)
    async with SessionLocal() as s:
        s.add(Deal(organization_id=1, contact_id=contact.id, name="Perdido", status="lost", closed_at=utcnow()))
        s.add(Deal(organization_id=1, contact_id=contact.id, name="Ganado", status="won", closed_at=utcnow()))
        await s.commit()
    await scheduler.poll_events(utcnow() + timedelta(seconds=1))
    e = await _enrollment(entry_j["id"], contact.id)
    assert e is not None and e.context["source"] == "event:deal_lost"
    g = await _enrollment(goal_j["id"], contact.id)
    assert g.status == "goal_met" and g.exit_reason == "deal_won"
    # Ya leído: el siguiente ciclo no vuelve a inscribir
    await scheduler.poll_events(utcnow() + timedelta(seconds=2))
    async with SessionLocal() as s:
        n = await s.scalar(sql("select count(*) from public.journey_enrollments where journey_id = :j"),
                           {"j": entry_j["id"]})
    assert n == 1
    assert scheduler.matches_filter({"pipeline": ["a", "b"]}, {"pipeline": "b"})
    assert not scheduler.matches_filter({"typification_id": 3}, {"typification_id": 4})


async def test_date_field_entry(client):
    c = client
    due_c, later_c = await _contact(), await _contact()
    today = datetime.now(ZoneInfo("America/Bogota")).date()
    async with SessionLocal() as s:
        for cid, due in ((due_c.id, today + timedelta(days=30)), (later_c.id, today + timedelta(days=31))):
            await s.execute(sql("insert into public.contact_vehicles (organization_id, contact_id, plate, make, "
                                "insurance_due, source) values (1, :c, :p, 'Mazda', :d, 'api')"),
                            {"c": cid, "d": due, "p": f"J{cid:05d}"})
        await s.commit()
    j = await _journey(c, _defn({"id": "x", "type": "exit"}),
                       {"type": "date_field", "field": "vehicle.insurance_due", "offset_days": -30, "at_time": "00:00"})
    async with SessionLocal() as s:
        ids = await scheduler.date_targets(s, await s.get(Journey, j["id"]), today)
    assert due_c.id in ids and later_c.id not in ids
    await scheduler.run_date_entries(utcnow())
    assert await _enrollment(j["id"], due_c.id) is not None
    async with SessionLocal() as s:
        n = await s.scalar(sql("select count(*) from public.journey_enrollments where journey_id = :j"), {"j": j["id"]})
    await scheduler.run_date_entries(utcnow())  # una vez al día
    async with SessionLocal() as s:
        assert await s.scalar(sql("select count(*) from public.journey_enrollments where journey_id = :j"),
                              {"j": j["id"]}) == n
    bad = await c.post("/api/journeys", json={"name": unique_name("Fecha mala"), "entry": {
        "type": "date_field", "field": "contact.name", "at_time": "10:00"}, "definition": _defn({"id": "x", "type": "exit"})})
    r = await c.post(f"/api/journeys/{bad.json()['id']}/publish")
    assert r.status_code == 422 and "no es un campo de fecha" in r.text


async def test_public_api_enroll_and_event(client):
    c = client
    key = (await c.post("/api/api-keys", json={"name": "Journeys", "scopes": ["journeys:write"]})).json()["key"]
    weak = (await c.post("/api/api-keys", json={"name": "Sin alcance", "scopes": ["contacts:read"]})).json()["key"]
    j = await _journey(c, _defn({"id": "w", "type": "wait", "config": {"days": 1}}), {"type": "api"})
    phone = unique_phone("5732")
    h = {"Authorization": f"Bearer {key}"}
    r = await c.post(f"/v1/journeys/{j['id']}/enroll", json={"phone": phone, "data": {"origen": "web"}}, headers=h)
    assert r.status_code == 200 and r.json()["enrolled"] is True, r.text
    assert (await c.post(f"/v1/journeys/{j['id']}/enroll", json={"phone": phone},
                         headers=h)).json()["enrolled"] is False
    assert (await c.post(f"/v1/journeys/{j['id']}/enroll", json={"phone": phone},
                         headers={"Authorization": f"Bearer {weak}"})).status_code == 403
    assert (await c.post("/v1/journeys/999999/enroll", json={"phone": phone}, headers=h)).status_code == 404

    form_j = await _journey(c, _defn({"id": "w", "type": "wait", "config": {"days": 1}}),
                            {"type": "event", "event": "form_submitted", "filter": {"form": "cotizar"}})
    r = await c.post("/v1/journeys/events", json={"event": "form_submitted", "phone": phone,
                                                   "data": {"form": "cotizar"}}, headers=h)
    assert r.status_code == 200 and r.json()["enrolled"] >= 1, r.text
    assert await _enrollment(form_j["id"], r.json()["contact_id"]) is not None
    assert (await c.post("/v1/journeys/events", json={"event": "nada", "phone": phone}, headers=h)).status_code == 422


# --- Clics y canal automático ---------------------------------------------------------------------------------------
async def test_click_tracking_branch(client):
    c = client
    contact = await _contact()
    j = await _journey(c, _defn(
        {"id": "b", "type": "branch", "config": {"condition": {"type": "clicked", "within_hours": 48}},
         "branches": {"yes": "y", "no": None}},
        {"id": "y", "type": "exit", "config": {"reason": "clic"}}))
    await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})
    assert await _run(j["id"], contact.id) == "waiting"
    e = await _enrollment(j["id"], contact.id)
    body = engine.tracked_text("Mira {{link:https://example.com/oferta?x=1}} ya", e, "b")
    assert "/j/c?t=" in body and "https://example.com" not in body
    token = body.split("/j/c?t=")[1].split(" ")[0]
    r = await c.get(f"/j/c?t={token}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://example.com/oferta?x=1"
    assert (await c.get(f"/j/c?t={token[:-2]}zz", follow_redirects=False)).status_code == 404
    assert await _run(j["id"], contact.id) == "exited"
    assert (await _enrollment(j["id"], contact.id)).exit_reason == "clic"


async def test_send_text_channel_selection(client):
    c = client
    contact = await _contact()
    j = await _journey(c, _defn({"id": "t", "type": "send_text", "config": {
        "text": "Hola {{nombre}}", "fallback_template": {"template_name": "promo", "language": "es",
                                                         "params": ["{{nombre}}", "algo"]}}}))
    # Fuera de la ventana de 24 h: plantilla de respaldo
    await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [contact.id]})
    await _run(j["id"], contact.id)
    assert _templates_to(contact.wa_id) == ["promo"]

    # Dentro de la ventana: texto libre
    other = await _contact()
    await c.post("/webhooks/whatsapp", json=text(other.wa_id, f"jt.{other.id}", "hola"))
    await settle(0.5)
    await c.post(f"/api/journeys/{j['id']}/enroll", json={"contact_ids": [other.id]})
    await _run(j["id"], other.id)
    assert any(to == other.wa_id and body.startswith("Hola") for to, body in WA.sent)
    assert _templates_to(other.wa_id) == []
