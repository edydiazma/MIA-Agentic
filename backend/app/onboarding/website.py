"""Importar la empresa desde su sitio web: se leen la portada y hasta 8 páginas del mismo dominio (nosotros,
contacto, productos, preguntas frecuentes…) y UNA llamada estructurada al Cortex extrae perfil, preguntas
frecuentes y productos. Nada se inventa: lo que no aparece en el sitio queda en null.

Seguridad: solo http(s), sin redes internas (cada redirección se revalida), tamaño y tiempo acotados.
"""

import asyncio
import html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx

from app.ai.router import json_call

MAX_PAGES = 8
MAX_BYTES = 1_500_000
PAGE_CHARS = 6000
TOTAL_CHARS = 32000
KEYWORDS = ("nosotros", "about", "quienes", "empresa", "contacto", "contact", "producto", "product", "servicio",
            "service", "faq", "pregunta", "precio", "price", "catalogo", "catalog", "tienda", "shop", "sede",
            "horario", "ubicacion")


class ImportError_(Exception):
    """Error de importación con mensaje para el usuario."""


def _check_host(url: str) -> str:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ImportError_("La dirección debe empezar por http:// o https://")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ImportError_(f"No se encontró el sitio {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ImportError_("La dirección apunta a una red interna")
    return url


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


async def fetch(url: str) -> tuple[str, str]:
    """(url final, html). Sigue hasta 3 redirecciones revalidando cada destino."""
    current = url
    async with httpx.AsyncClient(timeout=10, follow_redirects=False,
                                 headers={"User-Agent": "WA-Agent-Onboarding/1.0"}) as http:
        for _ in range(4):
            await asyncio.to_thread(_check_host, current)
            async with http.stream("GET", current) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ImportError_(f"El sitio respondió {r.status_code}")
                chunks, size = [], 0
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                    chunks.append(chunk)
                return current, b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
    raise ImportError_("Demasiadas redirecciones")


def to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?is)<(br|p|div|li|h[1-6]|tr|section|article)[^>]*>", "\n", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in page.split("\n")]
    return "\n".join(line for line in lines if line)


def title_and_meta(page: str) -> str:
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    desc = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', page)
    parts = []
    if title:
        parts.append("Título: " + html.unescape(title.group(1)).strip())
    if desc:
        parts.append("Descripción: " + html.unescape(desc.group(1)).strip())
    return "\n".join(parts)


def pick_links(base: str, page: str) -> list[str]:
    host = urlparse(base).hostname
    seen, ranked = set(), []
    for href in re.findall(r'(?i)href=["\']([^"\'#]+)', page):
        u = urljoin(base, href.strip())
        p = urlparse(u)
        if p.scheme not in ("http", "https") or p.hostname != host or re.search(r"\.(pdf|jpe?g|png|gif|zip|mp4)$", p.path, re.I):
            continue
        u = u.split("?")[0].rstrip("/")
        if u in seen or u == base.rstrip("/"):
            continue
        seen.add(u)
        score = sum(k in u.lower() for k in KEYWORDS)
        if score:
            ranked.append((score, u))
    ranked.sort(key=lambda x: -x[0])
    return [u for _s, u in ranked[:MAX_PAGES]]


_NULLABLE = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profile", "faqs", "products"],
    "properties": {
        "profile": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "description", "about", "address", "email", "phone", "website", "hours", "tone",
                         "vertical"],
            "properties": {
                "name": _NULLABLE, "description": _NULLABLE, "about": _NULLABLE, "address": _NULLABLE,
                "email": _NULLABLE, "phone": _NULLABLE, "website": _NULLABLE, "hours": _NULLABLE, "tone": _NULLABLE,
                "vertical": {"type": ["string", "null"],
                             "enum": ["automotriz", "salud", "educacion", "retail", "servicios", "inmobiliaria", "otro",
                                      None]},
            },
        },
        "faqs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["question", "answer"],
            "properties": {"question": {"type": "string"}, "answer": {"type": "string"}}}},
        "products": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "description", "price", "currency", "url", "image_url"],
            "properties": {"name": {"type": "string"}, "description": _NULLABLE, "price": {"type": ["number", "null"]},
                           "currency": _NULLABLE, "url": _NULLABLE, "image_url": _NULLABLE}}},
    },
}

SYSTEM = (
    "Extraes datos de una empresa a partir del texto de su sitio web para configurar su atención por WhatsApp. "
    "Reglas: usa SOLO información que aparezca en el texto; si un dato no está, devuelve null (nunca lo inventes). "
    "about: frase de máximo 139 caracteres. description: máximo 512 caracteres. tone: cómo se expresa la marca "
    "(p. ej. «cercano y profesional»). vertical: la industria más cercana. faqs: preguntas que un cliente haría con "
    "su respuesta tal como aparece en el sitio (máximo 15). products: productos o servicios con precio solo si "
    "está publicado (máximo 30). Responde en español."
)


async def import_site(org: int, url: str) -> dict:
    url = normalize_url(url)
    if not url:
        raise ImportError_("Indica la dirección del sitio web")
    try:
        base, home = await fetch(url)
    except httpx.HTTPError as e:
        raise ImportError_(f"No se pudo abrir el sitio: {type(e).__name__}") from e
    pages = [(base, title_and_meta(home) + "\n" + to_text(home))]
    for link in pick_links(base, home):
        try:
            final, page = await fetch(link)
            pages.append((final, to_text(page)))
        except (ImportError_, httpx.HTTPError):
            continue
    budget, corpus = TOTAL_CHARS, []
    for u, text in pages:
        chunk = text[: min(PAGE_CHARS, budget)]
        if not chunk:
            break
        corpus.append(f"### {u}\n{chunk}")
        budget -= len(chunk)
    data, _ctx = await json_call(org, None, "onboarding", SYSTEM,
                                 f"Sitio: {base}\n\n" + "\n\n".join(corpus), SCHEMA, max_tokens=6000)
    profile = data.get("profile") or {}
    profile["website"] = profile.get("website") or base
    return {"profile": {k: profile.get(k) for k in SCHEMA["properties"]["profile"]["required"]},
            "faqs": [f for f in data.get("faqs") or [] if f.get("question") and f.get("answer")][:15],
            "products": [p for p in data.get("products") or [] if p.get("name")][:30],
            "pages_read": len(pages)}
