"""Normalizadores de llaves del registro maestro (docs/data-model.md §15).

Cada normalizador recibe el valor tal como se capturó y devuelve un `Norm` con el valor normalizado (para buscar y
cruzar clientes), el subtipo canónico, datos estructurados y un factor de confianza (1 = formato verificado, < 1 =
plausible pero sin verificar). Devuelve None si el valor no sirve.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

CALLING_CODES = {"CO": "57", "MX": "52", "PE": "51", "CL": "56", "AR": "54", "EC": "593", "VE": "58", "PA": "507",
                 "CR": "506", "GT": "502", "SV": "503", "HN": "504", "NI": "505", "BO": "591", "PY": "595",
                 "UY": "598", "BR": "55", "DO": "1", "PR": "1", "US": "1", "CA": "1", "ES": "34"}
# Longitud del número nacional por país (sin indicativo)
NATIONAL_LEN = {"CO": (10,), "MX": (10,), "PE": (9, 8), "CL": (9,), "AR": (10, 11), "EC": (9, 8), "VE": (10,),
                "PA": (8, 7), "CR": (8,), "GT": (8,), "SV": (8,), "HN": (8,), "NI": (8,), "BO": (8,), "PY": (9,),
                "UY": (8, 9), "BR": (10, 11), "DO": (10,), "PR": (10,), "US": (10,), "CA": (10,), "ES": (9,)}

DOC_TYPES = {
    "CC": ("cc", "c.c", "c.c.", "cedula", "cedula de ciudadania", "cédula", "cédula de ciudadanía", "cedula ciudadania"),
    "CE": ("ce", "c.e", "c.e.", "cedula de extranjeria", "cédula de extranjería", "extranjeria"),
    "NIT": ("nit", "n.i.t", "n.i.t."),
    "PAS": ("pas", "pp", "pasaporte", "passport"),
    "TI": ("ti", "t.i", "t.i.", "tarjeta de identidad"),
    "RUT": ("rut", "r.u.t"),
    "DNI": ("dni", "documento nacional de identidad"),
    "RFC": ("rfc",),
    "CPF": ("cpf",),
    "CURP": ("curp",),
    "PPT": ("ppt", "permiso por proteccion temporal", "permiso por protección temporal", "pep"),
    "LIC": ("licencia", "licencia de conduccion", "licencia de conducción"),
}
_DOC_ALIASES = {alias: code for code, aliases in DOC_TYPES.items() for alias in aliases}
NETWORKS = {"whatsapp": ("whatsapp", "wa"), "instagram": ("instagram", "ig", "insta"),
            "facebook": ("facebook", "fb", "messenger"), "tiktok": ("tiktok", "tt"), "x": ("x", "twitter"),
            "linkedin": ("linkedin",), "telegram": ("telegram", "tg"), "youtube": ("youtube", "yt")}
_NET_ALIASES = {a: n for n, aliases in NETWORKS.items() for a in aliases}
_NET_HOSTS = {"instagram.com": "instagram", "facebook.com": "facebook", "fb.com": "facebook", "tiktok.com": "tiktok",
              "twitter.com": "x", "x.com": "x", "linkedin.com": "linkedin", "t.me": "telegram", "youtube.com": "youtube"}
MONTHS = {"enero": 1, "ene": 1, "january": 1, "jan": 1, "febrero": 2, "feb": 2, "february": 2, "marzo": 3,
          "mar": 3, "march": 3, "abril": 4, "abr": 4, "april": 4, "apr": 4, "mayo": 5, "may": 5, "junio": 6,
          "jun": 6, "june": 6, "julio": 7, "jul": 7, "july": 7, "agosto": 8, "ago": 8, "august": 8, "aug": 8,
          "septiembre": 9, "setiembre": 9, "sep": 9, "sept": 9, "set": 9, "september": 9, "octubre": 10, "oct": 10,
          "october": 10, "noviembre": 11, "nov": 11, "november": 11, "diciembre": 12, "dic": 12, "december": 12,
          "dec": 12}
NAME_PARTICLES = {"de", "del", "la", "las", "los", "y", "e", "da", "das", "do", "dos", "van", "von", "di", "san"}
CITIES = {
    "bogota": ("Bogotá", "CO"), "medellin": ("Medellín", "CO"), "cali": ("Cali", "CO"),
    "barranquilla": ("Barranquilla", "CO"), "cartagena": ("Cartagena", "CO"), "bucaramanga": ("Bucaramanga", "CO"),
    "pereira": ("Pereira", "CO"), "manizales": ("Manizales", "CO"), "cucuta": ("Cúcuta", "CO"),
    "ibague": ("Ibagué", "CO"), "santa marta": ("Santa Marta", "CO"), "villavicencio": ("Villavicencio", "CO"),
    "pasto": ("Pasto", "CO"), "neiva": ("Neiva", "CO"), "armenia": ("Armenia", "CO"), "monteria": ("Montería", "CO"),
    "valledupar": ("Valledupar", "CO"), "sincelejo": ("Sincelejo", "CO"), "popayan": ("Popayán", "CO"),
    "tunja": ("Tunja", "CO"), "chia": ("Chía", "CO"), "soacha": ("Soacha", "CO"), "envigado": ("Envigado", "CO"),
    "bello": ("Bello", "CO"), "itagui": ("Itagüí", "CO"), "rionegro": ("Rionegro", "CO"), "zipaquira": ("Zipaquirá", "CO"),
    "palmira": ("Palmira", "CO"), "buenaventura": ("Buenaventura", "CO"), "duitama": ("Duitama", "CO"),
    "sogamoso": ("Sogamoso", "CO"), "girardot": ("Girardot", "CO"), "floridablanca": ("Floridablanca", "CO"),
    "cdmx": ("Ciudad de México", "MX"), "ciudad de mexico": ("Ciudad de México", "MX"),
    "guadalajara": ("Guadalajara", "MX"), "monterrey": ("Monterrey", "MX"), "lima": ("Lima", "PE"),
    "santiago": ("Santiago", "CL"), "quito": ("Quito", "EC"), "guayaquil": ("Guayaquil", "EC"),
    "buenos aires": ("Buenos Aires", "AR"), "caracas": ("Caracas", "VE"), "panama": ("Panamá", "PA"),
    "san jose": ("San José", "CR"), "madrid": ("Madrid", "ES"), "miami": ("Miami", "US"),
}
COUNTRIES = {"colombia": "CO", "mexico": "MX", "peru": "PE", "chile": "CL", "argentina": "AR", "ecuador": "EC",
             "venezuela": "VE", "panama": "PA", "costa rica": "CR", "espana": "ES", "estados unidos": "US",
             "usa": "US", "brasil": "BR", "bolivia": "BO", "paraguay": "PY", "uruguay": "UY", "guatemala": "GT"}
ADDRESS_ABBR = [
    (r"\b(carrera|cra|kra|kr|cr|carr)\b\.?", "Cra"), (r"\b(calle|cll|cl|clle)\b\.?", "Calle"),
    (r"\b(avenida carrera|ak)\b\.?", "Av. Cra"), (r"\b(avenida calle|ac)\b\.?", "Av. Calle"),
    (r"\b(avenida|avda|av)\b\.?", "Av"), (r"\b(diagonal|dg|diag)\b\.?", "Dg"),
    (r"\b(transversal|tv|tr|transv|trans)\b\.?", "Tv"), (r"\b(circular|cir|circ)\b\.?", "Cir"),
    (r"\b(autopista|autop|aut)\b\.?", "Autopista"), (r"\b(kilometro|km)\b\.?", "Km"),
    (r"\b(apartamento|apto|apt|ap)\b\.?", "Apto"), (r"\b(interior|int)\b\.?", "Int"),
    (r"\b(torre|tor)\b\.?", "Torre"), (r"\b(bloque|blq|bl)\b\.?", "Bloque"), (r"\b(oficina|of|ofi)\b\.?", "Of"),
    (r"\b(barrio|br|bro)\b\.?", "Barrio"), (r"\b(numero|nro|num|no)\b\.?\s*", "# "),
]
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-']+@[a-z0-9.\-]+\.[a-z]{2,}$")
VIN_TRANSLIT = {**{str(d): d for d in range(10)}, "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
                "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9, "S": 2, "T": 3, "U": 4, "V": 5, "W": 6,
                "X": 7, "Y": 8, "Z": 9}
VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
NIT_WEIGHTS = (3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71)


@dataclass
class Norm:
    value: str  # valor normalizado
    subtype: str | None = None
    data: dict = field(default_factory=dict)
    factor: float = 1.0  # 1 = formato verificado


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def fold(s: str | None) -> str:
    """Texto comparable: minúsculas, sin tildes ni signos, espacios simples."""
    s = strip_accents((s or "").lower())
    return " ".join(re.sub(r"[^a-z0-9#]+", " ", s).split())


def keyify(s: str | None) -> str:
    """Nombre de campo comparable: "Nro. de documento" → "nro_de_documento"."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", strip_accents((s or "").lower()))).strip("_")


# --- Teléfono -------------------------------------------------------------------------------------
def phone(value: str, country: str = "CO") -> Norm | None:
    raw = (value or "").strip()
    if not raw:
        return None
    country = (country or "CO").upper()
    cc = CALLING_CODES.get(country, "57")
    intl = raw.startswith("+") or raw.startswith("00")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    kind = None
    if intl:
        if not 8 <= len(digits) <= 15:
            return None
        if country == "CO" and digits.startswith("57"):
            kind = _co_kind(digits[2:])
        return Norm(digits, subtype=kind, factor=1.0 if (kind or not digits.startswith("57")) else 0.8)
    if country == "CO":
        if len(digits) == 12 and digits.startswith("57") and _co_kind(digits[2:]):
            return Norm(digits, subtype=_co_kind(digits[2:]))
        if len(digits) == 10 and _co_kind(digits):
            return Norm(cc + digits, subtype=_co_kind(digits))
        # Fijo con formato anterior a 2021: indicativo de un dígito + 7 dígitos ("(1) 345 6789")
        old = re.match(r"^\(?([1-8])\)?[\s.-]*(\d{3})[\s.-]*(\d{4})$", raw)
        if old or (len(digits) == 8 and digits[0] in "12345678"):
            d = (old.group(1) + old.group(2) + old.group(3)) if old else digits
            return Norm(cc + "60" + d, subtype="fijo", factor=0.9)
        if len(digits) == 7:  # fijo sin indicativo de ciudad: no se puede saber la ciudad
            return Norm(cc + digits, subtype="fijo", factor=0.5)
        return None
    if len(digits) in NATIONAL_LEN.get(country, (10,)):
        return Norm(cc + digits, factor=0.9)
    if digits.startswith(cc) and 8 <= len(digits) <= 15:
        return Norm(digits, factor=0.9)
    if 8 <= len(digits) <= 15:
        return Norm(digits, factor=0.5)
    return None


def _co_kind(national: str) -> str | None:
    if len(national) == 10 and national.startswith("3"):
        return "movil"
    if len(national) == 10 and national.startswith("60") and national[2] in "12345678":
        return "fijo"
    return None


# --- Correo y usuarios ----------------------------------------------------------------------------
def email(value: str) -> Norm | None:
    v = (value or "").strip().lower()
    v = re.sub(r"^mailto:", "", v).strip(" <>.,;")
    if " " in v:  # "Jhon vanegas50@gmail.com": el correo es la palabra con @
        v = next((w for w in v.split() if "@" in w), v.replace(" ", ""))
    return Norm(v) if EMAIL_RE.match(v) else None


def username(value: str, network: str | None = None) -> Norm | None:
    raw = (value or "").strip()
    if not raw:
        return None
    net = _NET_ALIASES.get((network or "").strip().lower(), (network or "").strip().lower() or None)
    m = re.search(r"(?:https?://)?(?:www\.|m\.)?([a-z0-9.\-]+\.[a-z]{2,})/@?([A-Za-z0-9_.\-]+)", raw)
    if m and m.group(1).lower() in _NET_HOSTS:
        net = net or _NET_HOSTS[m.group(1).lower()]
        raw = m.group(2)
    v = raw.lstrip("@").strip().lower().rstrip("/")
    if not re.fullmatch(r"[a-z0-9_.\-]{1,64}", v):
        return None
    return Norm(v, subtype=net)


# --- Documento de identidad -----------------------------------------------------------------------
def doc_type(value: str | None) -> str | None:
    v = (value or "").strip().lower()
    if not v:
        return None
    if v.upper() in DOC_TYPES:
        return v.upper()
    return _DOC_ALIASES.get(v) or _DOC_ALIASES.get(strip_accents(v))


def nit_dv(base: str) -> int:
    total = sum(int(d) * w for d, w in zip(reversed(base), NIT_WEIGHTS, strict=False))
    r = total % 11
    return r if r in (0, 1) else 11 - r


def _cpf_ok(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def _rut_dv(body: str) -> str:
    s, m = 0, 2
    for d in reversed(body):
        s += int(d) * m
        m = 2 if m == 7 else m + 1
    r = 11 - s % 11
    return {11: "0", 10: "K"}.get(r, str(r))


def document(value: str, subtype: str | None = None) -> Norm | None:
    raw = (value or "").strip()
    if not raw:
        return None
    t = doc_type(subtype)
    if not t:  # "CC 1.020.345.678", "NIT: 900.123.456-7"
        m = re.match(r"^\s*([A-Za-zÁÉÍÓÚáéíóú.\s]{2,30}?)[\s:#.\-]*(?=[\dA-Z])", raw)
        if m and doc_type(m.group(1).strip(" .:")):
            t = doc_type(m.group(1).strip(" .:"))
            raw = raw[m.end():]
    t = t or "CC"
    if t in ("PAS", "CURP", "RFC", "PPT"):
        v = re.sub(r"[^A-Z0-9]", "", raw.upper())
        if t == "CURP" and not re.fullmatch(r"[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d", v):
            return Norm(v, subtype=t, factor=0.6) if len(v) == 18 else None
        if t == "RFC" and not re.fullmatch(r"[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}", v):
            return None
        return Norm(v, subtype=t) if 5 <= len(v) <= 20 else None
    if t == "RUT":
        v = re.sub(r"[^0-9Kk]", "", raw).upper()
        if len(v) < 2:
            return None
        body, dv = v[:-1], v[-1]
        return Norm(body, subtype=t, data={"dv": dv}, factor=1.0 if _rut_dv(body) == dv else 0.5)
    if t == "NIT":
        m = re.match(r"^\s*([\d.\s]+?)\s*-\s*(\d)\s*$", raw)
        base = re.sub(r"\D", "", m.group(1) if m else raw)
        if not 6 <= len(base) <= 15:
            return None
        if m:
            dv = int(m.group(2))
            return Norm(base, subtype=t, data={"dv": dv}, factor=1.0 if nit_dv(base) == dv else 0.4)
        return Norm(base, subtype=t, data={"dv": nit_dv(base)}, factor=0.9)
    digits = re.sub(r"\D", "", raw)
    if t == "CPF":
        return Norm(digits, subtype=t, factor=1.0 if _cpf_ok(digits) else 0.4) if len(digits) == 11 else None
    if not 5 <= len(digits) <= 15:
        return None
    return Norm(digits, subtype=t)


# --- Placa y VIN ----------------------------------------------------------------------------------
def plate(value: str) -> Norm | None:
    v = re.sub(r"[^A-Z0-9]", "", strip_accents((value or "").upper()))
    if not 4 <= len(v) <= 8:
        return None
    if re.fullmatch(r"[A-Z]{3}\d{3}", v):
        return Norm(v, data={"pattern": "car_co"})
    if re.fullmatch(r"[A-Z]{3}\d{2}[A-Z]", v):
        return Norm(v, data={"pattern": "moto_co"})
    if re.fullmatch(r"[A-Z]{2}\d{4}", v) or re.fullmatch(r"\d{3}[A-Z]{3}", v):
        return Norm(v, data={"pattern": "special_co"}, factor=0.9)
    return Norm(v, data={"pattern": "other"}, factor=0.7) if re.search(r"\d", v) and re.search(r"[A-Z]", v) else None


def vin_check_ok(v: str) -> bool:
    total = sum(VIN_TRANSLIT[c] * w for c, w in zip(v, VIN_WEIGHTS, strict=True))
    r = total % 11
    return v[8] == ("X" if r == 10 else str(r))


def vin(value: str) -> Norm | None:
    v = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    if len(v) != 17 or re.search(r"[IOQ]", v):
        return None
    ok = vin_check_ok(v)
    # Muchos VIN fuera de Norteamérica no usan dígito de control: válido pero con algo menos de confianza
    return Norm(v, data={"check_digit": ok, "wmi": v[:3], "model_year_code": v[9]}, factor=1.0 if ok else 0.85)


# --- Nombres --------------------------------------------------------------------------------------
def title_name(value: str) -> str:
    words = " ".join((value or "").replace("_", " ").split()).split(" ")
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in NAME_PARTICLES:
            out.append(lw)
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in lw.split("-")))
    return " ".join(x for x in out if x)


def name(value: str) -> Norm | None:
    v = title_name(re.sub(r"[^\w\s'\-.]", " ", value or "", flags=re.UNICODE))
    v = re.sub(r"\d", "", v).strip(" .-")
    if len(v) < 2:
        return None
    return Norm(fold(v), data={"display": " ".join(v.split())})


def split_full_name(full: str) -> tuple[str | None, str | None]:
    """Nombres y apellidos de un nombre hispano (dos apellidos al final). Las partículas van con la palabra
    siguiente: "María de los Ángeles Pérez Gómez" → ("María de los Ángeles", "Pérez Gómez"). Con 3 bloques se asume
    1 nombre + 2 apellidos (lo habitual en documentos); con 4 o más, los 2 últimos son los apellidos."""
    v = title_name(full)
    if not v:
        return None, None
    # Agrupa partículas con la palabra siguiente: "de los Ángeles" → un bloque
    tokens, buf = [], []
    for w in v.split():
        buf.append(w)
        if w.lower() not in NAME_PARTICLES:
            tokens.append(" ".join(buf))
            buf = []
    if buf:
        tokens.append(" ".join(buf))
    n = len(tokens)
    if n == 1:
        return tokens[0], None
    if n == 2:
        return tokens[0], tokens[1]
    if n == 3:
        return tokens[0], " ".join(tokens[1:])
    return " ".join(tokens[:-2]), " ".join(tokens[-2:])


# --- Fechas ---------------------------------------------------------------------------------------
def parse_date(value: str, day_first: bool = True) -> date | None:
    iso = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$", value or "")
    if iso:
        return _mk(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    v = fold(value).replace("#", "")
    if not v:
        return None
    m = re.fullmatch(r"(\d{4}) (\d{1,2}) (\d{1,2})(?: .*)?", v)  # 1990-03-12 (y con hora)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2}) (\d{1,2}) (\d{2,4})", v)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        d, mo = (a, b) if day_first or b > 12 else (b, a)
        if mo > 12 and d <= 12:
            d, mo = mo, d
        return _mk(y, mo, d)
    words = v.replace(" de ", " ").replace(" del ", " ").split()
    nums = [w for w in words if w.isdigit()]
    mon = next((MONTHS[w] for w in words if w in MONTHS), None)
    if mon and len(nums) >= 2:
        a, b = nums[0], nums[1]
        d, y = (int(a), _year(b)) if len(a) <= 2 else (int(b), _year(a))
        return _mk(y, mon, d)
    return None


def _year(s: str) -> int:
    y = int(s)
    if len(s) == 2:
        y += 1900 if y > (date.today().year % 100) + 5 else 2000
    return y


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if 1900 <= y <= 2100 else None
    except ValueError:
        return None


def date_value(value: str) -> Norm | None:
    d = parse_date(value)
    return Norm(d.isoformat()) if d else None


# --- Dirección ------------------------------------------------------------------------------------
def address(value: str, subtype: str | None = None) -> Norm | None:
    raw = " ".join((value or "").split())
    if len(raw) < 5:
        return None
    line = raw
    for pattern, repl in ADDRESS_ABBR:
        line = re.sub(pattern, repl, line, flags=re.IGNORECASE)
    line = re.sub(r"\s*#\s*", " # ", line)
    line = re.sub(r"\s*-\s*", "-", line)
    line = " ".join(line.split()).strip(" ,")
    folded = fold(raw)
    city = country = None
    for key in sorted(CITIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", folded):
            city, country = CITIES[key]
            break
    for key, code in COUNTRIES.items():
        if re.search(rf"\b{re.escape(key)}\b", folded):
            country = code
            break
    kind = (subtype or "").strip().lower() or None
    kind = {"hogar": "casa", "residencia": "casa", "oficina": "trabajo", "entrega": "envio", "envío": "envio",
            "facturacion": "facturacion", "facturación": "facturacion"}.get(kind, kind)
    has_number = bool(re.search(r"\d", raw))
    return Norm(fold(line), subtype=kind, data={"line": line, "city": city, "country": country},
                factor=1.0 if has_number else 0.6)


def text(value: str) -> Norm | None:
    v = " ".join((value or "").split())
    return Norm(fold(v), data={"display": v}) if v else None


def normalize(normalizer: str, value, subtype: str | None = None, country: str = "CO") -> Norm | None:
    """Despacha al normalizador del tipo de llave. Nunca lanza: valores inválidos → None."""
    if value is None:
        return None
    value = str(value)
    try:
        if normalizer == "phone":
            n = phone(value, country)
            if n and subtype and (subtype or "").lower() not in ("movil", "fijo"):
                n.subtype = subtype.lower()  # whatsapp, trabajo… (lo dicho manda sobre lo inferido)
            return n
        if normalizer == "email":
            return email(value)
        if normalizer == "username":
            return username(value, subtype)
        if normalizer == "document":
            return document(value, subtype)
        if normalizer == "plate":
            return plate(value)
        if normalizer == "vin":
            return vin(value)
        if normalizer == "name":
            return name(value)
        if normalizer == "date":
            return date_value(value)
        if normalizer == "address":
            return address(value, subtype)
        n = text(value)
        if n and subtype:
            n.subtype = subtype
        return n
    except Exception:  # noqa: BLE001 — un valor raro nunca debe romper la extracción
        return None
