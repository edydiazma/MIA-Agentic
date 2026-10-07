"""No inventar precios ni disponibilidad.

Las sugerencias solo pueden mencionar cifras de dinero que aparezcan en el contexto (catálogo, conocimiento, la
conversación). La disponibilidad («hay stock», «está disponible») solo se afirma si el contexto trae productos
del catálogo. Lo que no pase la validación se descarta y queda registrado en la sugerencia (flags).
"""

import re

# $ 120.000.000 · COP 85,000 · 120 millones · USD 25.000 · 45.900.000
MONEY_RE = re.compile(
    r"(?:(?:\$|cop|usd|us\$|€|mxn|pen|clp)\s*\d[\d.,]*(?:\s*(?:millones|mil|k|m))?"
    r"|\d[\d.,]*\s*(?:millones|mil(?:lones)?|pesos|d[oó]lares|cop|usd))",
    re.IGNORECASE)
AVAILABILITY_RE = re.compile(
    r"\b(?:tenemos disponib\w*|est[aá] disponible|est[aá]n disponibles|hay (?:stock|existencias|unidades)|"
    r"en stock|disponibilidad inmediata|lo tenemos)\b", re.IGNORECASE)


def _amount(token: str) -> float | None:
    """'$ 120.000.000' → 120000000; '120 millones' → 120000000; '85,5 mil' → 85500."""
    t = token.lower()
    mult = 1_000_000 if "millon" in t else (1_000 if re.search(r"\bmil\b|\dk\b", t) else 1)
    digits = re.sub(r"[^\d.,]", "", t)
    if not digits:
        return None
    if mult > 1:  # en «120,5 millones» la coma es decimal
        num = digits.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", digits):  # separadores de miles
        num = re.sub(r"[.,]", "", digits)
    else:
        num = digits.replace(",", ".")
    try:
        return float(num) * mult
    except ValueError:
        return None


def amounts(text: str) -> set[float]:
    out = set()
    for m in MONEY_RE.finditer(text or ""):
        v = _amount(m.group(0))
        if v is not None:
            out.add(round(v, 2))
    # También números grandes sueltos del contexto (precios del catálogo sin símbolo: 45900000)
    for m in re.finditer(r"\b\d{1,3}(?:[.,]\d{3}){1,4}\b|\b\d{5,}\b", text or ""):
        v = _amount(m.group(0))
        if v is not None:
            out.add(round(v, 2))
    return out


def check(text: str, context_text: str, has_catalog: bool) -> list[str]:
    """Motivos por los que la sugerencia no debe mostrarse (vacío = válida)."""
    flags = []
    allowed = amounts(context_text)
    for m in MONEY_RE.finditer(text or ""):
        v = _amount(m.group(0))
        if v is not None and round(v, 2) not in allowed:
            flags.append(f"precio no respaldado: {m.group(0).strip()}")
    if not has_catalog and AVAILABILITY_RE.search(text or ""):
        flags.append("afirma disponibilidad sin datos del catálogo")
    return flags
