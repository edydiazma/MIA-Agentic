"""Rúbricas por defecto y validación de criterios."""

import re
import unicodedata

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QAScorecard

AGENT_SCORECARD = {
    "name": "Atención de asesores",
    "applies_to": "agent",
    "sample_pct": 100,
    "criteria": [
        {"key": "saludo_empatia", "label": "Saludo y empatía", "weight": 15, "critical": False,
         "description": "Saluda, se presenta, usa el nombre del cliente y muestra interés genuino."},
        {"key": "entendimiento", "label": "Entendimiento de la necesidad", "weight": 20, "critical": False,
         "description": "Hace preguntas para entender qué necesita el cliente antes de ofrecer."},
        {"key": "informacion_correcta", "label": "Información correcta", "weight": 25, "critical": True,
         "description": "Lo que informa (precios, condiciones, disponibilidad) es correcto y no promete lo que no "
                        "puede cumplir."},
        {"key": "objeciones", "label": "Manejo de objeciones", "weight": 15, "critical": False,
         "description": "Responde dudas y objeciones con argumentos, sin presionar ni ignorarlas."},
        {"key": "cierre", "label": "Cierre y siguiente paso", "weight": 15, "critical": False,
         "description": "Propone un siguiente paso concreto (cita, cotización, compra) y lo confirma."},
        {"key": "tiempos", "label": "Tiempos de respuesta", "weight": 10, "critical": False,
         "description": "Responde a tiempo según las horas de los mensajes; no deja al cliente esperando."},
    ],
}

BOT_SCORECARD = {
    "name": "Calidad del bot",
    "applies_to": "bot",
    "sample_pct": 25,
    "criteria": [
        {"key": "relevancia", "label": "Respuesta relevante", "weight": 30, "critical": False,
         "description": "Responde lo que el cliente preguntó, de forma clara y breve."},
        {"key": "no_inventa", "label": "No inventa datos", "weight": 35, "critical": True,
         "description": "No inventa precios, productos, horarios ni políticas que no estén en su información."},
        {"key": "transferencia", "label": "Transfiere a tiempo", "weight": 20, "critical": False,
         "description": "Pasa a un asesor cuando el cliente lo pide o cuando no puede resolver, sin dar vueltas."},
        {"key": "tono", "label": "Tono de marca", "weight": 15, "critical": False,
         "description": "Amable, profesional y coherente con la marca."},
    ],
}

DEFAULTS = (AGENT_SCORECARD, BOT_SCORECARD)


async def ensure_defaults(session: AsyncSession, org: int) -> bool:
    """Crea las rúbricas por defecto si la empresa no tiene ninguna. True si creó."""
    if await session.scalar(select(QAScorecard.id).where(QAScorecard.organization_id == org).limit(1)):
        return False
    for d in DEFAULTS:
        session.add(QAScorecard(organization_id=org, name=d["name"], applies_to=d["applies_to"],
                                criteria=d["criteria"], sample_pct=d["sample_pct"]))
    await session.flush()
    return True


def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40]


def clean_criteria(criteria: list) -> list[dict]:
    """Valida y normaliza los criterios de una rúbrica (422 si no son válidos)."""
    if not isinstance(criteria, list) or not criteria:
        raise HTTPException(422, "La rúbrica necesita al menos un criterio")
    if len(criteria) > 15:
        raise HTTPException(422, "Máximo 15 criterios por rúbrica")
    out, keys = [], set()
    for c in criteria:
        if not isinstance(c, dict) or not str(c.get("label", "")).strip():
            raise HTTPException(422, "Cada criterio necesita un nombre")
        key = _slug(str(c.get("key") or c["label"]))
        if not key or key in keys:
            raise HTTPException(422, f"Criterio repetido o inválido: {c.get('label')}")
        try:
            weight = int(c.get("weight", 0))
        except (TypeError, ValueError):
            raise HTTPException(422, f"Peso inválido en «{c['label']}»") from None
        if not 0 <= weight <= 100:
            raise HTTPException(422, f"El peso de «{c['label']}» debe estar entre 0 y 100")
        keys.add(key)
        out.append({"key": key, "label": str(c["label"]).strip()[:80],
                    "description": str(c.get("description") or "").strip()[:500], "weight": weight,
                    "critical": bool(c.get("critical"))})
    if sum(c["weight"] for c in out) <= 0:
        raise HTTPException(422, "La suma de los pesos debe ser mayor que 0")
    return out
