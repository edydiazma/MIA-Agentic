"""Texto para la base de conocimiento: limpieza, idioma, tokens, fragmentos y datos personales."""

import hashlib
import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4  # estimación suficiente para presupuestos (español/inglés)
TARGET_TOKENS = 600  # fragmentos de ~400–800 tokens
MAX_TOKENS = 800
MIN_TOKENS = 400
OVERLAP = 0.15

_ES = {"de", "la", "que", "el", "en", "los", "las", "por", "con", "para", "una", "del", "se", "es", "su", "al",
       "como", "más", "pero", "sus", "le", "ya", "o", "este", "sí", "porque", "esta", "entre", "cuando", "muy"}
_EN = {"the", "of", "and", "to", "in", "is", "you", "that", "it", "for", "on", "are", "with", "as", "was", "be",
       "this", "have", "from", "or", "by", "not", "but", "what", "all", "were", "we", "when", "your", "can"}


def tokens(text: str) -> int:
    return max(1, len(text or "") // CHARS_PER_TOKEN)


def checksum(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def clean(text: str) -> str:
    """Normaliza espacios y saltos de línea; quita caracteres de control y líneas repetidas seguidas."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    lines, prev = [], None
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line and line == prev:
            continue
        lines.append(line)
        prev = line if line else prev
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def language(text: str) -> str | None:
    words = re.findall(r"[a-záéíóúñü]+", (text or "")[:5000].lower())
    if len(words) < 5:
        return None
    es = sum(w in _ES for w in words)
    en = sum(w in _EN for w in words)
    if es == en == 0:
        return None
    return "es" if es >= en else "en"


# --- Fragmentación ----------------------------------------------------------------------------------------------
@dataclass
class Chunk:
    ordinal: int
    heading: str | None
    content: str
    tokens: int


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")


def _is_heading(line: str) -> str | None:
    m = _MD_HEADING.match(line)
    if m:
        return m.group(2).strip()
    s = line.strip()
    # Títulos en MAYÚSCULAS cortos o líneas cortas terminadas en ":" (manuales, PDF)
    if 3 <= len(s) <= 80 and not s.endswith((".", ",")) and (
            (s.isupper() and any(c.isalpha() for c in s)) or (s.endswith(":") and len(s.split()) <= 8)):
        return s.rstrip(":").strip()
    return None


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?¿¡;])\s+", text)
    return [p for p in parts if p.strip()]


def _split_long(paragraph: str, max_chars: int) -> list[str]:
    """Un párrafo más largo que el máximo se corta por oraciones (o por palabras si una oración no cabe)."""
    out, cur = [], ""
    for sent in _sentences(paragraph):
        if len(sent) > max_chars:  # oración gigante (tablas, listas sin puntos)
            words, buf = sent.split(), ""
            for w in words:
                if len(buf) + len(w) + 1 > max_chars and buf:
                    out.append(buf)
                    buf = ""
                buf = f"{buf} {w}".strip()
            if buf:
                sent = buf
            else:
                continue
        if cur and len(cur) + len(sent) + 1 > max_chars:
            out.append(cur)
            cur = ""
        cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def _tail(text: str, chars: int) -> str:
    """Final del fragmento anterior para el solapamiento, empezando en un límite de palabra."""
    if chars <= 0 or len(text) <= chars:
        return text if chars > 0 else ""
    piece = text[-chars:]
    cut = piece.find(" ")
    return piece[cut + 1:] if 0 <= cut < len(piece) - 1 else piece


def chunk_text(text: str, title: str | None = None, target_tokens: int = TARGET_TOKENS,
               max_tokens: int = MAX_TOKENS, overlap: float = OVERLAP) -> list[Chunk]:
    """Fragmentos por títulos y párrafos de ~target tokens (máx. max_tokens) con solapamiento entre fragmentos
    consecutivos de la misma sección. El título de la sección acompaña a cada fragmento (para citar)."""
    text = clean(text)
    if not text:
        return []
    target_c, max_c = target_tokens * CHARS_PER_TOKEN, max_tokens * CHARS_PER_TOKEN
    overlap_c = int(target_c * overlap)

    # Secciones: (título, [párrafos])
    sections: list[tuple[str | None, list[str]]] = [(title, [])]
    para: list[str] = []

    def flush_para():
        if para:
            sections[-1][1].append(" ".join(para))
            para.clear()

    for line in text.split("\n"):
        if not line.strip():
            flush_para()
            continue
        heading = _is_heading(line)
        if heading:
            flush_para()
            sections.append((heading, []))
            continue
        para.append(line.strip())
    flush_para()

    chunks: list[Chunk] = []
    for heading, paragraphs in sections:
        if not paragraphs:
            continue
        pieces: list[str] = []
        for p in paragraphs:
            pieces.extend(_split_long(p, max_c - overlap_c) if len(p) > max_c - overlap_c else [p])
        cur, prev = "", ""
        for piece in pieces:
            if cur and len(cur) + len(piece) + 2 > target_c:
                chunks.append(Chunk(len(chunks), heading, cur, tokens(cur)))
                prev = cur
                cur = _tail(prev, overlap_c)
            cur = f"{cur}\n\n{piece}".strip() if cur else piece
        if cur and cur != _tail(prev, overlap_c):
            chunks.append(Chunk(len(chunks), heading, cur, tokens(cur)))
    return chunks


# --- Datos personales ---------------------------------------------------------------------------------------------
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(r"(?<![\w$])\+?\d[\d\s().\-]{6,}\d(?![\w])")
_PLATE = re.compile(r"\b[A-Z]{3}[\s\-]?\d{2}[0-9A-Z]\b")
_MONEY_AFTER = re.compile(r"^\s*(cop|usd|pesos|millones|mil|k\b|%)", re.I)


def scrub_pii(text: str, country: str = "CO") -> str:
    """Quita correos, teléfonos, documentos y placas de un texto (conversaciones que se aprenden). Usa los
    normalizadores del registro maestro para no confundir precios con teléfonos."""
    from app.golden import normalize as gn

    text = _EMAIL.sub("[correo]", text or "")

    def _num(m: re.Match) -> str:
        raw = m.group(0)
        start, end = m.start(), m.end()
        before = text[max(0, start - 2):start]
        if "$" in before or _MONEY_AFTER.match(text[end:end + 10]):
            return raw  # es un precio
        digits = re.sub(r"\D", "", raw)
        if gn.phone(raw, country):
            return "[teléfono]"
        if 6 <= len(digits) <= 12:
            return "[documento]"
        return raw

    text = _PHONEISH.sub(_num, text)
    text = _PLATE.sub(lambda m: "[placa]" if gn.plate(m.group(0)) else m.group(0), text)
    return text
