"""Mensajes disparadores: enlace corto con tracking, Click to WA por anuncio, texto sin código, toques y acciones."""

from urllib.parse import parse_qs, unquote, urlparse

from sqlalchemy import select, text as sql, update

from app.db import SessionLocal
from app.models import Attribution, AttributionTouch, Channel, Conversation, WebSession
from tests.conftest import WA, settle, text
from tests.test_flows import _new_flow, b, fake_buttons  # noqa: F401  (fixture autouse del módulo de flujos)


async def _attr(phone: str) -> Attribution:
    async with SessionLocal() as s:
        return (await s.scalars(select(Attribution).join(Conversation, Conversation.id == Attribution.conversation_id)
                                .where(Conversation.contact.has(wa_id=phone))
                                .order_by(Attribution.id.desc()).limit(1))).first()


async def _touches(conv_id: int) -> list[AttributionTouch]:
    async with SessionLocal() as s:
        return list((await s.scalars(select(AttributionTouch).where(AttributionTouch.conversation_id == conv_id)
                                     .order_by(AttributionTouch.occurred_at))).all())


async def test_trigger_links_end_to_end(client):
    c = client
    async with SessionLocal() as s:
        await s.execute(update(Channel).values(display_phone="+57 300 111 2222"))
        await s.commit()
    flow_id = await _new_flow(c, "Bienvenida CX-5", "nuncacoincide-cx5", [b("w1", "send_text", {"text": "¡Hola! Te cuento de la CX-5"})])
    # El flujo se inicia por el enlace (su único script tiene disparador de palabra clave: se usa el primero)

    r = await c.post("/api/wa-links", json={
        "name": "Promo CX-5 Meta", "platform": "meta_ads", "trigger_text": "Hola, quiero la promo de la CX-5",
        "meta_ad_ids": ["AD-111"], "tags": ["Promo CX5"], "flow_id": flow_id})
    assert r.status_code == 200, r.text
    meta_link = r.json()
    assert meta_link["utm_source"] == "facebook" and meta_link["utm_medium"] == "paid_social"
    assert meta_link["utm_campaign"] == "promo_cx_5_meta"
    assert meta_link["direct_url"].startswith("https://wa.me/573001112222?text=Hola%2C%20quiero")
    assert meta_link["short_url"].endswith(f"/t/l/{meta_link['slug']}")

    r = await c.post("/api/wa-links", json={"name": "Google búsqueda", "platform": "google_ads",
                                            "trigger_text": "Hola, vi su anuncio de financiación", "slug": "g-financia"})
    assert r.status_code == 200, r.text
    g_link = r.json()
    assert g_link["google_final_url_suffix"] == "utm_source=google&utm_medium=cpc&utm_campaign=google_busqueda"

    # Validaciones: mismo texto (con otras mayúsculas/signos) → 409; texto corto → 422; slug repetido → 409
    same = await c.post("/api/wa-links", json={"name": "Copia", "trigger_text": "Hola quiero la promo de la CX-5!!"})
    assert same.status_code == 409, same.text
    assert (await c.post("/api/wa-links", json={"name": "x", "trigger_text": "Hola"})).status_code == 422
    assert (await c.post("/api/wa-links", json={"name": "y", "trigger_text": "Otro texto distinto largo",
                                                "slug": "g-financia"})).status_code == 409

    # 1) Click to WhatsApp: referral del anuncio AD-111 → enlace por anuncio, etiquetas y flujo
    p1 = "573150000001"
    await c.post("/webhooks/whatsapp", json=text(p1, "wl.1", "Hola, quiero la promo de la CX-5", referral={
        "source_type": "ad", "source_id": "AD-111", "ctwa_clid": "CLID-1", "source_url": "https://fb.me/x"}))
    await settle()
    a1 = await _attr(p1)
    assert (a1.channel, a1.matched_by, a1.link_id, a1.ctwa_clid) == ("meta_ctwa", "ctwa_referral", meta_link["id"],
                                                                    "CLID-1")
    assert a1.utm_source == "facebook" and a1.touches == 1 and a1.first_touch_id
    assert WA.sent[-1] == (p1, "¡Hola! Te cuento de la CX-5")
    conv1 = (await c.get("/api/conversations", params={"q": p1})).json()[0]
    assert "promo cx5" in conv1["tags"]

    # 2) Enlace corto con gclid → visita + código en el texto → google_ads por ref_code
    r = await c.get(f"/t/l/{g_link['slug']}", params={"gclid": "GCLID-9", "utm_content": "anuncio-a"},
                    follow_redirects=False)
    assert r.status_code == 302
    target = urlparse(r.headers["location"])
    assert target.netloc == "wa.me" and target.path == "/573001112222"
    prefilled = parse_qs(target.query)["text"][0]
    assert prefilled.startswith("Hola, vi su anuncio de financiación (ref: ")
    p2 = "573150000002"
    await c.post("/webhooks/whatsapp", json=text(p2, "wl.2", unquote(prefilled)))
    await settle()
    a2 = await _attr(p2)
    assert (a2.channel, a2.matched_by, a2.link_id, a2.gclid) == ("google_ads", "ref_code", g_link["id"], "GCLID-9")
    assert (a2.utm_source, a2.utm_content) == ("google", "anuncio-a")  # la URL manda; el enlace completa
    async with SessionLocal() as s:
        ws = await s.get(WebSession, (a2.web_session_id, a2.web_session_at))
        assert ws.link_id == g_link["id"] and ws.site_id is None and ws.wa_click_at

    # 3) El cliente borró el código: se reconoce por el texto
    p3 = "573150000003"
    await c.post("/webhooks/whatsapp", json=text(p3, "wl.3", "hola!! vi su anuncio de financiacion, me interesa"))
    await settle()
    a3 = await _attr(p3)
    assert (a3.channel, a3.matched_by, a3.link_id) == ("google_ads", "trigger_text", g_link["id"])
    assert a3.gclid is None

    # 4) Vuelve por un anuncio de Meta: toque nuevo (último toque = Meta); repetir el mismo origen no suma
    await c.post("/webhooks/whatsapp", json=text(p3, "wl.4", "Hola, quiero la promo de la CX-5", referral={
        "source_type": "ad", "source_id": "AD-111", "ctwa_clid": "CLID-3"}))
    await settle()
    await c.post("/webhooks/whatsapp", json=text(p3, "wl.5", "Hola, quiero la promo de la CX-5", referral={
        "source_type": "ad", "source_id": "AD-111", "ctwa_clid": "CLID-3"}))
    await settle()
    a3b = await _attr(p3)
    assert a3b.id == a3.id and a3b.touches == 2 and a3b.channel == "meta_ctwa" and a3b.ctwa_clid == "CLID-3"
    touches = await _touches(a3.conversation_id)
    assert [(t.channel, t.is_first) for t in touches] == [("google_ads", True), ("meta_ctwa", False)]

    # 5) Un mensaje normal no crea toques
    await c.post("/webhooks/whatsapp", json=text(p3, "wl.6", "¿Cuál es el precio?"))
    await settle()
    assert len(await _touches(a3.conversation_id)) == 2

    # 6) Prueba de texto y rollup diario por enlace
    m = (await c.post("/api/wa-links/test-match", json={"text": "Hola quiero la promo de la cx-5 (ref: ABCDEF)"})).json()
    assert m["link"]["id"] == meta_link["id"] and m["ref_code"] == "ABCDEF"
    async with SessionLocal() as s:
        await s.execute(sql("select reporting.refresh_range(1, current_date - 1, current_date + 1)"))
        await s.commit()
    links = {x["id"]: x for x in (await c.get("/api/wa-links")).json()}
    assert links[g_link["id"]]["stats"]["clicks"] == 1
    assert links[g_link["id"]]["stats"]["conversations"] == 1  # p3 pasó a Meta (último toque)
    assert links[meta_link["id"]]["stats"]["conversations"] == 2

    # 7) Enlace pausado: abre WhatsApp sin crear visita
    body = {k: g_link[k] for k in ("name", "platform", "trigger_text")} | {"is_active": False}
    assert (await c.put(f"/api/wa-links/{g_link['id']}", json=body)).status_code == 200
    r = await c.get(f"/t/l/{g_link['slug']}", follow_redirects=False)
    assert r.status_code == 302 and "ref" not in unquote(r.headers["location"])
    assert (await c.get("/t/l/no-existe", follow_redirects=False)).status_code == 404
    await c.post(f"/api/flows/{flow_id}/pause")
