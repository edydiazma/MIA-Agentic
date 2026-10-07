"""Stripe: llave, webhook registrado con los eventos necesarios y precios de los planes."""

from sqlalchemy import select

from app.models import Plan
from app.preflight.core import Context, Result, check, error_text, mask, skipped

STRIPE_API = "https://api.stripe.com/v1"
REQUIRED_EVENTS = ("checkout.session.completed", "customer.subscription.updated", "customer.subscription.deleted",
                   "invoice.paid", "invoice.payment_failed")


def _events_ok(enabled: list[str]) -> list[str]:
    if "*" in enabled:
        return []
    have = set(enabled)
    return [e for e in REQUIRED_EVENTS if e not in have and not (
        e.startswith("customer.subscription.") and "customer.subscription.*" in have)]


@check("stripe", "stripe.account")
async def stripe(ctx: Context) -> list[Result]:
    s = ctx.settings
    if not s.stripe_secret_key:
        return skipped("stripe", "stripe.account", "Stripe",
                       "Sin STRIPE_SECRET_KEY: el cobro de planes está desactivado (modo instalación propia).")
    auth = (s.stripe_secret_key, "")
    out: list[Result] = []
    live = s.stripe_secret_key.startswith(("sk_live_", "rk_live_"))
    async with ctx.http() as http:
        r = await http.get(f"{STRIPE_API}/account", auth=auth)
        if r.status_code >= 400:
            return [Result("stripe", "stripe.account", "Stripe: llave", "fail",
                           f"Stripe rechazó la llave ({error_text(r)}): copia la «Secret key» desde Developers → API keys.",
                           {"key": mask(s.stripe_secret_key)})]
        acct = r.json()
        charges = bool(acct.get("charges_enabled"))
        out.append(Result("stripe", "stripe.account", "Stripe: llave", "pass" if charges or not live else "warn",
                          f"Cuenta {acct.get('id')} en modo {'producción' if live else 'prueba'}." + (
                              "" if charges or not live else " La cuenta aún no puede cobrar: completa la "
                                                             "activación en Stripe."),
                          {"key": mask(s.stripe_secret_key), "live": live, "charges_enabled": charges}))
        # Webhook
        url = f"{s.public_base_url.rstrip('/')}/api/billing/webhook"
        r = await http.get(f"{STRIPE_API}/webhook_endpoints", params={"limit": 100}, auth=auth)
        endpoints = (r.json().get("data") or []) if r.status_code < 400 else []
        ep = next((e for e in endpoints if e.get("url") == url), None)
        if not ep:
            out.append(Result("stripe", "stripe.webhook", "Stripe: webhook", "fail",
                              f"No hay un webhook hacia {url}: créalo en Developers → Webhooks con los eventos "
                              f"{', '.join(REQUIRED_EVENTS)}.", {"url": url}))
        else:
            missing = _events_ok(ep.get("enabled_events") or [])
            problems = []
            if ep.get("status") != "enabled":
                problems.append("el webhook está deshabilitado")
            if missing:
                problems.append("faltan eventos: " + ", ".join(missing))
            if not s.stripe_webhook_secret:
                problems.append("falta STRIPE_WEBHOOK_SECRET (Signing secret del webhook)")
            out.append(Result("stripe", "stripe.webhook", "Stripe: webhook", "fail" if problems else "pass",
                              ("; ".join(problems).capitalize() + ".") if problems else "Webhook activo con los eventos.",
                              {"url": url, "events": ep.get("enabled_events")}))
        # Precios de los planes
        async with ctx.db() as session:
            plans = (await session.scalars(select(Plan).where(Plan.is_public))).all()
        for p in plans:
            key = f"stripe.price:{p.key}"
            label = f"Stripe: precio del plan {p.name}"
            if not p.provider_price_id:
                out.append(Result("stripe", key, label, "warn",
                                  f"El plan {p.name} no tiene precio de Stripe: créalo en Stripe y guarda su id en el "
                                  "back-office (/plataforma → Planes).", {"plan": p.key}))
                continue
            r = await http.get(f"{STRIPE_API}/prices/{p.provider_price_id}", auth=auth)
            ok = r.status_code < 400 and r.json().get("active")
            out.append(Result("stripe", key, label, "pass" if ok else "fail",
                              "Precio activo." if ok else
                              f"El precio {p.provider_price_id} no existe o está inactivo en esta cuenta de Stripe "
                              "(¿llave de prueba con precio de producción?).",
                              {"plan": p.key, "price_id": p.provider_price_id}))
    return out
