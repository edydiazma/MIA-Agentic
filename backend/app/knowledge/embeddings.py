"""Embeddings y rerank de la base de conocimiento (1024 dimensiones).

Proveedores:
- Voyage AI (`voyage-3.5`, `voyage-3.5-lite`, …): POST /v1/embeddings {input, model, input_type: document|query,
  output_dimension: 1024}; rerank POST /v1/rerank {query, documents, model: rerank-2.5, top_k}.
- OpenAI (`text-embedding-3-small` / `-large`) u OpenAI-compatible / Azure: POST /embeddings {input, model,
  dimensions: 1024}.

Conexión: la conexión de IA indicada en ajustes `knowledge.embedding_connection_id` (proveedor voyage u openai*,
modelo = modelo de embeddings, clave en Vault); si no hay, la clave del servidor (VOYAGE_API_KEY, luego
OPENAI_API_KEY). Cada lote queda en ai_calls (propósito «embedding») con tokens y costo.
Las pruebas usan `FakeEmbedder` (determinista, sin red) vía `set_override`.
"""

import asyncio
import hashlib
import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

log = logging.getLogger(__name__)

DIMS = 1024
BATCH_SIZE = 96
BATCH_TOKENS = 100_000
MAX_RETRIES = 4
TIMEOUT_S = 60
VOYAGE_URL = "https://api.voyageai.com/v1"
OPENAI_URL = "https://api.openai.com/v1"
# USD por millón de tokens (si la conexión no define su propio costo)
PRICES = {"voyage-3.5": 0.06, "voyage-3.5-lite": 0.02, "voyage-3-large": 0.18, "voyage-4": 0.06,
          "voyage-4-lite": 0.02, "voyage-4-large": 0.12, "text-embedding-3-small": 0.02,
          "text-embedding-3-large": 0.13, "rerank-2.5": 0.05, "rerank-2.5-lite": 0.02}
_limits: dict[str, asyncio.Semaphore] = {}


class EmbeddingError(Exception):
    pass


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    model: str
    tokens: int


class Embedder:
    provider = "base"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None,
                 connection_id: int | None = None, cost_per_mtok: float | None = None):
        self.model, self.api_key, self.base_url = model, api_key, base_url
        self.connection_id = connection_id
        self.cost_per_mtok = cost_per_mtok if cost_per_mtok is not None else PRICES.get(model, 0.0)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    async def embed_batch(self, texts: list[str], input_type: str) -> EmbedResult:  # pragma: no cover
        raise NotImplementedError

    async def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]] | None:
        return None  # solo Voyage tiene rerank


async def _post(url: str, headers: dict, body: dict) -> dict:
    """POST con reintentos (429 / 5xx / red) respetando Retry-After."""
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
                r = await http.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            if attempt == MAX_RETRIES:
                raise EmbeddingError(f"Error de red: {e}") from e
        else:
            if r.status_code < 400:
                return r.json()
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == MAX_RETRIES:
                detail = r.text[:300]
                raise EmbeddingError(f"HTTP {r.status_code}: {detail}")
            retry_after = r.headers.get("retry-after")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                delay = max(delay, float(retry_after))
        await asyncio.sleep(min(delay, 30))
        delay *= 2
    raise EmbeddingError("Sin respuesta")  # pragma: no cover


class VoyageEmbedder(Embedder):
    provider = "voyage"

    async def embed_batch(self, texts: list[str], input_type: str) -> EmbedResult:
        body = {"input": texts, "model": self.model, "input_type": input_type, "output_dimension": DIMS,
                "truncation": True}
        data = await _post(f"{(self.base_url or VOYAGE_URL).rstrip('/')}/embeddings",
                           {"Authorization": f"Bearer {self.api_key}"}, body)
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        return EmbedResult([r["embedding"] for r in rows], data.get("model") or self.model,
                           int((data.get("usage") or {}).get("total_tokens") or 0))

    async def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]] | None:
        data = await _post(f"{(self.base_url or VOYAGE_URL).rstrip('/')}/rerank",
                           {"Authorization": f"Bearer {self.api_key}"},
                           {"query": query, "documents": documents, "model": "rerank-2.5", "top_k": top_k,
                            "truncation": True})
        return [(int(d["index"]), float(d["relevance_score"])) for d in data.get("data") or []]


class OpenAIEmbedder(Embedder):
    provider = "openai"

    async def embed_batch(self, texts: list[str], input_type: str) -> EmbedResult:
        base = (self.base_url or OPENAI_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if "azure" in base:
            headers = {"api-key": self.api_key or ""}
        data = await _post(f"{base}/embeddings", headers, {"input": texts, "model": self.model, "dimensions": DIMS})
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        return EmbedResult([r["embedding"] for r in rows], data.get("model") or self.model,
                           int((data.get("usage") or {}).get("total_tokens") or 0))


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


class FakeEmbedder(Embedder):
    """Determinista y sin red (pruebas): bolsa de raíces de palabras proyectada a 1024 dimensiones."""

    provider = "fake"

    def __init__(self, model: str = "fake-embed"):
        super().__init__(model, cost_per_mtok=0.02)
        self.calls: list[tuple[str, int]] = []

    @staticmethod
    def vector(text: str) -> list[float]:
        v = [0.0] * DIMS
        words = re.findall(r"[a-z0-9]+", _fold(text))
        for w in words:
            stem = w[:6]
            h = int.from_bytes(hashlib.sha256(stem.encode()).digest()[:8], "big")
            v[h % DIMS] += 1.0 if (h >> 20) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed_batch(self, texts: list[str], input_type: str) -> EmbedResult:
        self.calls.append((input_type, len(texts)))
        return EmbedResult([self.vector(t) for t in texts], self.model, sum(max(1, len(t) // 4) for t in texts))

    async def rerank(self, query: str, documents: list[str], top_k: int) -> list[tuple[int, float]] | None:
        q = self.vector(query)
        scored = [(i, sum(a * b for a, b in zip(q, self.vector(d), strict=False))) for i, d in enumerate(documents)]
        return sorted(scored, key=lambda x: -x[1])[:top_k]


_override: Embedder | None = None


def set_override(embedder: Embedder | None) -> None:
    """Pruebas: fuerza un proveedor (FakeEmbedder) para todas las empresas."""
    global _override
    _override = embedder


def _make(provider: str, model: str, api_key: str | None, base_url: str | None,
          connection_id: int | None = None, cost: float | None = None) -> Embedder | None:
    if provider == "voyage":
        return VoyageEmbedder(model or "voyage-3.5", api_key, base_url, connection_id, cost)
    if provider in ("openai", "openai_compatible", "azure_openai"):
        return OpenAIEmbedder(model or "text-embedding-3-small", api_key, base_url, connection_id, cost)
    return None


async def get_embedder(session: AsyncSession, org: int, *, rerank: bool = False) -> Embedder | None:
    """Proveedor de embeddings (o de rerank) de la empresa; None si no hay ninguno configurado."""
    if _override is not None:
        return _override
    from app.ai.router import resolve_connection
    from app.models import AIConnection
    from app.settings_store import get_setting

    cfg = await get_setting(session, "knowledge", org)
    conn_id = cfg.get("rerank_connection_id" if rerank else "embedding_connection_id")
    if conn_id:
        conn = await session.get(AIConnection, int(conn_id))
        if conn and conn.organization_id == org and conn.is_active:
            resolved = await resolve_connection(session, conn)
            key = resolved.api_key or (get_settings().voyage_api_key if conn.provider == "voyage" else None)
            emb = _make(conn.provider, conn.model, key, conn.base_url, conn.id,
                        float(conn.input_cost_per_mtok) if conn.input_cost_per_mtok is not None else None)
            if emb and emb.api_key:
                return emb
    if rerank:
        return None  # el rerank solo se usa si se configuró explícitamente
    env = get_settings()
    if env.voyage_api_key:
        return VoyageEmbedder("voyage-3.5", env.voyage_api_key)
    if env.openai_api_key:
        return OpenAIEmbedder("text-embedding-3-small", env.openai_api_key)
    return None


def _batches(texts: list[str]) -> list[list[int]]:
    out, cur, cur_tokens = [], [], 0
    for i, t in enumerate(texts):
        n = max(1, len(t) // 4)
        if cur and (len(cur) >= BATCH_SIZE or cur_tokens + n > BATCH_TOKENS):
            out.append(cur)
            cur, cur_tokens = [], 0
        cur.append(i)
        cur_tokens += n
    if cur:
        out.append(cur)
    return out


async def _log_call(org: int, emb: Embedder, status: str, latency_ms: int, tokens: int, error: str | None,
                    call_ids: list[int] | None = None) -> None:
    """Costo y latencia en ai_calls (sesión propia: no toca la transacción del llamador)."""
    from app.db import SessionLocal
    from app.models import AICall

    try:
        async with SessionLocal() as s:
            call = AICall(organization_id=org, connection_id=emb.connection_id, purpose="embedding", attempt=1,
                          status=status, latency_ms=latency_ms, input_tokens=tokens or None, output_tokens=0,
                          cost_usd=round(tokens * emb.cost_per_mtok / 1_000_000, 6) if tokens else None,
                          error=(error or "")[:2000] or None)
            s.add(call)
            await s.commit()
            if call_ids is not None:
                call_ids.append(call.id)
    except Exception:  # noqa: BLE001 — el registro nunca rompe la indexación
        log.debug("No se pudo registrar la llamada de embeddings", exc_info=True)


async def embed(emb: Embedder, org: int, texts: list[str], input_type: str = "document",
                call_ids: list[int] | None = None) -> EmbedResult:
    """Embeddings por lotes con límite de concurrencia por proveedor. Lanza EmbeddingError si falla."""
    if not texts:
        return EmbedResult([], emb.model, 0)
    sem = _limits.setdefault(emb.key, asyncio.Semaphore(2))
    vectors: list[list[float] | None] = [None] * len(texts)
    total, model = 0, emb.model
    for idx in _batches(texts):
        started = time.monotonic()
        try:
            async with sem:
                res = await emb.embed_batch([texts[i][:32000] for i in idx], input_type)
        except EmbeddingError as e:
            await _log_call(org, emb, "error", int((time.monotonic() - started) * 1000), 0, str(e), call_ids)
            raise
        if len(res.vectors) != len(idx) or any(len(v) != DIMS for v in res.vectors):
            await _log_call(org, emb, "invalid", int((time.monotonic() - started) * 1000), res.tokens,
                            "dimensiones inesperadas", call_ids)
            raise EmbeddingError(f"El proveedor devolvió vectores de {len(res.vectors[0]) if res.vectors else 0} "
                                 f"dimensiones (se esperan {DIMS})")
        await _log_call(org, emb, "ok", int((time.monotonic() - started) * 1000), res.tokens, None, call_ids)
        for i, v in zip(idx, res.vectors, strict=True):
            vectors[i] = v
        total += res.tokens
        model = res.model
    return EmbedResult([v for v in vectors if v is not None], model, total)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if a is None or b is None:
        return 0.0
    a, b = list(a), list(b)
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
