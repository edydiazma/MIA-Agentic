"""Entorno de pruebas: Postgres creado desde supabase/migrations; WhatsApp y Cortex (IA) simulados."""

import asyncio
import itertools
import os
import tempfile

from tests.pg_setup import ensure_test_database

tmp = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL=ensure_test_database(),
    MEDIA_DIR=f"{tmp}/media",
    ORGANIZATION_ID="1",
    WA_PHONE_NUMBER_ID="PNID",
    WA_WABA_ID="WABA",
    WA_APP_SECRET="",
    DEBOUNCE_SECONDS="0.05",
    BOT_MERGE_WINDOW_S="0.05",
    ADMIN_EMAIL="admin@test.com",
    ADMIN_PASSWORD="secret",
    JWT_SECRET="x" * 40,
    # Aislar las pruebas del .env real: nada llama a WhatsApp, Supabase ni proveedores de IA de verdad.
    SUPABASE_URL="",
    SUPABASE_SECRET_KEY="",
    WA_VERIFY_TOKEN="verify-me",
    WA_ACCESS_TOKEN="test-token",
    ANTHROPIC_API_KEY="test-no-real-calls",
    OPENAI_API_KEY="",
    VOYAGE_API_KEY="",
    DEFAULT_MODEL="claude-opus-5-5",
    DEFAULT_EFFORT="low",
    HISTORY_LIMIT="30",
    MAX_IMAGES_IN_CONTEXT="3",
    TRANSCRIBE_MODEL="",
)

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.ai import router as ai_router  # noqa: E402
from app.ai.base import AgentResult, ImagePart  # noqa: E402
from app.main import app, bootstrap  # noqa: E402
from app.whatsapp import WhatsAppClient  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000") + b"\x00" * 20

TEMPLATES = [
    {"name": "promo", "language": "es", "status": "APPROVED", "category": "MARKETING",
     "components": [{"type": "BODY", "text": "Hola {{1}}, tenemos {{2}} para ti"}]},
    {"name": "recordatorio", "language": "es", "status": "APPROVED", "category": "UTILITY",
     "components": [{"type": "HEADER", "format": "TEXT", "text": "Recordatorio"},
                    {"type": "BODY", "text": "Tu cita es el {{fecha}}"}], "parameter_format": "NAMED"},
]


class WA:
    """Registro de lo que se envió a WhatsApp."""

    sent: list[tuple[str, str]] = []
    templates_sent: list[tuple[str, str, list]] = []


class FakeChat:
    """Reemplaza al Cortex: responde según el último texto del cliente y usa las herramientas reales."""

    requests: list = []

    @staticmethod
    async def run_chat(session, cx, req, execute_tool, ctx):
        FakeChat.requests.append(req)
        last = req.turns[-1]
        if any(isinstance(p, ImagePart) for p in last.parts):
            return AgentResult(text="Veo tu imagen 👀")
        texto = " ".join(getattr(p, "text", "") for p in last.parts).lower()
        if "falla total" in texto:
            raise ai_router.CortexUnavailable("todas las conexiones fallaron", [])
        if "asesor" in texto:
            group = "ventas" if "ventas" in texto else ""
            await execute_tool("transfer_to_human", {"reason": "pidió asesor", "group": group})
            return AgentResult(text="Te paso con un asesor.")
        if texto.startswith("cita "):
            day = texto.split()[1]
            slots = await execute_tool("check_availability", {"date": day})
            first = slots.split(": ")[1].split(",")[0].strip()
            out = await execute_tool("book_appointment", {"date": day, "time": first, "notes": "Test drive"})
            return AgentResult(text=out)
        return AgentResult(text="¡Hola! ¿En qué te ayudo?")


class FakeCopilot:
    """Copiloto simulado por defecto (ninguna prueba llama a un modelo real). tests/test_copilot.py lo reemplaza."""

    calls: list = []

    @staticmethod
    async def call_json(org, feature, system, user, schema, conversation_id=None, max_tokens=1500):
        FakeCopilot.calls.append((feature, user))
        props = schema.get("properties", {})
        if "suggestions" in props:
            return {"suggestions": [{"text": "¡Hola! Con gusto te ayudo."}],
                    "next_action": {"action": "none", "label": "", "reason": "", "confidence": 0,
                                    "payload": {k: None for k in props["next_action"]["properties"]["payload"]["required"]}}}, None, 5
        if "need" in props:
            return {"need": "—", "captured_data": [], "bot_promises": [], "sentiment": "neutral", "next_step": "—"}, None, 5
        if "summary" in props:
            return {"summary": "Resumen", "outcome": "—"}, None, 5
        return {"text": "Texto"}, None, 5


counter = itertools.count()  # IDs de WhatsApp únicos en toda la sesión de pruebas
_phones = itertools.count(1)


def unique_phone(prefix: str = "5739") -> str:
    """Teléfono que ninguna otra prueba de la sesión usa (las pruebas comparten la empresa 1)."""
    return f"{prefix}{next(_phones):08d}"


def unique_name(base: str) -> str:
    """Nombre único en la sesión (evita que «Luis» de una prueba coincida con el de otra)."""
    return f"{base} {next(_phones)}"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    async def send_text(self, to, body):
        WA.sent.append((to, body))
        return [f"wamid.out{next(counter)}"]

    async def send_template(self, to, name, language, components):
        WA.templates_sent.append((to, name, components))
        return f"wamid.tpl{next(counter)}"

    async def send_image_link(self, to, link, caption=None):
        WA.sent.append((to, f"[img] {caption}"))
        return f"wamid.img{next(counter)}"

    async def list_templates(self, waba_id):
        return TEMPLATES

    async def download_media(self, media_id):
        return PNG, "image/png"

    async def noop(self, *a, **k):
        return None

    monkeypatch.setattr(WhatsAppClient, "send_text", send_text)
    monkeypatch.setattr(WhatsAppClient, "send_template", send_template)
    monkeypatch.setattr(WhatsAppClient, "send_image_link", send_image_link)
    monkeypatch.setattr(WhatsAppClient, "list_templates", list_templates)
    monkeypatch.setattr(WhatsAppClient, "download_media", download_media)
    monkeypatch.setattr(WhatsAppClient, "mark_read", noop)
    monkeypatch.setattr(ai_router, "run_chat", FakeChat.run_chat)
    from app.copilot import hooks as copilot_hooks, llm as copilot_llm

    copilot_hooks.DELAY_OVERRIDE = 0
    monkeypatch.setattr(copilot_llm, "call_json", FakeCopilot.call_json)


@pytest.fixture(autouse=True)
async def _reset_login_limits():
    """Los intentos fallidos de inicio de sesión cuentan por IP (15 min) y todas las pruebas comparten la IP del
    cliente de pruebas: sin esto, las pruebas que verifican un 401 terminan bloqueando el login de las siguientes."""
    from sqlalchemy import text as sql

    from app.db import SessionLocal

    async with SessionLocal() as s:
        await s.execute(sql("delete from public.rate_limit_counters where bucket like 'login_fail:%'"))
        await s.commit()
    yield


@pytest.fixture
async def client():
    await bootstrap()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/auth/login", json={"email": "admin@test.com", "password": "secret"})
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        c.token = r.json()["access_token"]
        yield c


def inbound(wa_id: str, msg_id: str, body: dict, name: str = "Ana", referral: dict | None = None) -> dict:
    msg = {"from": wa_id, "id": msg_id, **body}
    if referral:
        msg["referral"] = referral
    return {"entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "PNID"},
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
        "messages": [msg],
    }}]}]}


def text(wa_id: str, msg_id: str, body: str, **kw) -> dict:
    return inbound(wa_id, msg_id, {"type": "text", "text": {"body": body}}, **kw)


async def settle(seconds: float = 0.4):
    await asyncio.sleep(seconds)


async def eventually(fetch, check, timeout: float = 5.0, interval: float = 0.1):
    """Espera a que `check(await fetch())` sea verdadero (trabajo en segundo plano); devuelve el último valor si
    se cumple, None si se agota el tiempo. Evita esperas fijas que fallan cuando la máquina está cargada."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await fetch()
        if check(value):
            return value if value else True
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(interval)


def pytest_sessionfinish(session, exitstatus):
    """Borra la base temporal de esta ejecución de pruebas."""
    from tests.pg_setup import drop_test_database

    drop_test_database()
