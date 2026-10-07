"""SSO (OIDC / SAML), Web Push (VAPID) y voz (STUN, nodos)."""

import asyncio
import base64
import os
import re
import secrets
import socket
import struct
from datetime import timedelta

from sqlalchemy import select

from app.models import SSOConnection, WorkerHeartbeat, utcnow
from app.preflight.core import Context, Result, check, error_text, skipped

CERT_RE = re.compile(r"<(?:\w+:)?X509Certificate>\s*([A-Za-z0-9+/=\s]+?)\s*</(?:\w+:)?X509Certificate>")


# --- SSO --------------------------------------------------------------------------------------------------
def cert_expiry(pem_or_b64: str):
    from cryptography import x509

    text = pem_or_b64.strip()
    if "BEGIN CERTIFICATE" in text:
        cert = x509.load_pem_x509_certificate(text.encode())
    else:
        cert = x509.load_der_x509_certificate(base64.b64decode(re.sub(r"\s+", "", text)))
    return cert.not_valid_after_utc


def _cert_result(key: str, label: str, certs: list[str], data: dict) -> Result:
    if not certs:
        return Result("sso", key, label, "fail", "No se encontró el certificado de firma del IdP (X509Certificate).",
                      data)
    try:
        expiry = min(cert_expiry(c) for c in certs)
    except Exception as e:  # noqa: BLE001
        return Result("sso", key, label, "fail", f"Certificado ilegible ({type(e).__name__}).", data)
    days = (expiry - utcnow()).days
    data["cert_expires"] = expiry.date().isoformat()
    if days < 0:
        return Result("sso", key, label, "fail",
                      f"El certificado del IdP venció el {expiry:%Y-%m-%d}: nadie podrá entrar con SSO. Descarga el "
                      "certificado nuevo del IdP y actualízalo en Configuraciones → Seguridad.", data)
    if days < 30:
        return Result("sso", key, label, "warn",
                      f"El certificado del IdP vence en {days} días ({expiry:%Y-%m-%d}): rota el certificado antes.",
                      data)
    return Result("sso", key, label, "pass", f"Metadatos válidos; el certificado vence el {expiry:%Y-%m-%d}.", data)


@check("sso", "sso.connections", scope="org")
async def sso(ctx: Context) -> list[Result]:
    async with ctx.db() as s:
        conns = (await s.scalars(select(SSOConnection).where(
            SSOConnection.organization_id == ctx.organization_id, SSOConnection.is_active))).all()
    if not conns:
        return skipped("sso", "sso.connections", "Inicio de sesión único", "No hay conexiones SSO activas.")
    out: list[Result] = []
    async with ctx.http() as http:
        for c in conns:
            key, label, data = f"sso.connection:{c.id}", f"SSO «{c.name}» ({c.protocol.upper()})", {"slug": c.slug}
            if c.protocol == "oidc":
                r = await http.get(f"{(c.issuer or '').rstrip('/')}/.well-known/openid-configuration")
                if r.status_code >= 400:
                    out.append(Result("sso", key, label, "fail",
                                      f"No se pudo leer la configuración OIDC del emisor ({error_text(r)}): revisa el "
                                      "issuer (debe coincidir exactamente con el del IdP).", data))
                    continue
                doc = r.json()
                if doc.get("issuer", "").rstrip("/") != (c.issuer or "").rstrip("/"):
                    out.append(Result("sso", key, label, "fail",
                                      f"El issuer del IdP ({doc.get('issuer')}) no coincide con el configurado.", data))
                    continue
                r = await http.get(doc.get("jwks_uri", ""))
                keys = (r.json().get("keys") or []) if r.status_code < 400 else []
                out.append(Result("sso", key, label, "pass" if keys else "fail",
                                  f"Descubrimiento OIDC y {len(keys)} llaves de firma disponibles." if keys else
                                  "El JWKS del IdP no tiene llaves: no se podrán validar los id_token.",
                                  {**data, "keys": len(keys)}))
            else:
                certs = [c.idp_certificate] if c.idp_certificate else []
                if c.idp_metadata_url:
                    r = await http.get(c.idp_metadata_url)
                    if r.status_code >= 400:
                        out.append(Result("sso", key, label, "fail",
                                          f"No se pudo descargar la metadata SAML ({error_text(r)}).", data))
                        continue
                    certs = CERT_RE.findall(r.text) or certs
                out.append(_cert_result(key, label, certs, data))
    return out


# --- Web Push ---------------------------------------------------------------------------------------------
def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@check("push", "push.vapid")
async def vapid(ctx: Context) -> list[Result]:
    label = "Notificaciones push (VAPID)"
    s = ctx.settings
    if not (s.vapid_public_key and s.vapid_private_key):
        return skipped("push", "push.vapid", label,
                       "Sin VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY: la app del asesor no recibe notificaciones. Genéralas "
                       "con `npx web-push generate-vapid-keys`.")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        if "BEGIN" in s.vapid_private_key:
            priv = serialization.load_pem_private_key(s.vapid_private_key.encode(), password=None)
        else:
            priv = ec.derive_private_key(int.from_bytes(_b64url(s.vapid_private_key), "big"), ec.SECP256R1())
        pub = priv.public_key().public_bytes(serialization.Encoding.X962,
                                             serialization.PublicFormat.UncompressedPoint)
        sig = priv.sign(b"preflight", ec.ECDSA(hashes.SHA256()))
        priv.public_key().verify(sig, b"preflight", ec.ECDSA(hashes.SHA256()))
    except Exception as e:  # noqa: BLE001
        return [Result("push", "push.vapid", label, "fail",
                       f"VAPID_PRIVATE_KEY no es una llave P-256 válida ({type(e).__name__}).")]
    if pub != _b64url(s.vapid_public_key):
        return [Result("push", "push.vapid", label, "fail",
                       "VAPID_PUBLIC_KEY no corresponde a VAPID_PRIVATE_KEY: genera el par de nuevo y define ambas.")]
    subject_ok = s.vapid_subject.startswith(("mailto:", "https://")) and "example.com" not in s.vapid_subject
    return [Result("push", "push.vapid", label, "pass" if subject_ok else "warn",
                   "Par de llaves válido." + ("" if subject_ok else
                                              " Cambia VAPID_SUBJECT por un correo o URL real de tu empresa."))]


# --- Voz --------------------------------------------------------------------------------------------------
def stun_probe(host: str, port: int, timeout: float = 3.0) -> str | None:
    """Binding Request STUN (RFC 5389). Devuelve la IP pública vista por el servidor STUN, o None."""
    tid = secrets.token_bytes(12)
    req = struct.pack("!HHI12s", 0x0001, 0, 0x2112A442, tid)
    for fam, _, _, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[:1]:
        with socket.socket(fam, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(req, addr)
            data, _ = sock.recvfrom(2048)
        if len(data) < 20 or data[8:20] != tid:
            return None
        i = 20
        while i + 4 <= len(data):
            atype, alen = struct.unpack("!HH", data[i:i + 4])
            val = data[i + 4:i + 4 + alen]
            if atype == 0x0020 and len(val) >= 8:  # XOR-MAPPED-ADDRESS (IPv4)
                ip = struct.unpack("!I", val[4:8])[0] ^ 0x2112A442
                return socket.inet_ntoa(struct.pack("!I", ip))
            i += 4 + alen + (-alen % 4)
        return "?"
    return None


@check("voice", "voice.stun")
async def stun(ctx: Context) -> list[Result]:
    urls = [u.strip() for u in os.environ.get("VOICE_STUN_URLS", "stun:stun.l.google.com:19302").split(",")
            if u.strip()]
    if not urls:
        return skipped("voice", "voice.stun", "STUN (voz)", "Sin servidores STUN configurados (VOICE_STUN_URLS).")
    from app.preflight import checks_misc

    out = []
    for i, u in enumerate(urls[:3]):
        m = re.match(r"^stuns?:([^:?]+)(?::(\d+))?", u)
        if not m:
            continue
        host, port = m.group(1), int(m.group(2) or 3478)
        try:
            ip = await asyncio.to_thread(checks_misc.stun_probe, host, port)
        except Exception:  # noqa: BLE001
            ip = None
        out.append(Result("voice", f"voice.stun:{i}", f"STUN {host}:{port}", "pass" if ip else "fail",
                          f"Responde; IP pública vista: {ip}." if ip else
                          "Sin respuesta UDP: el security group / firewall bloquea la salida UDP. Sin STUN el agente "
                          "de voz de IA no establece audio.", {"public_ip": ip}))
    return out


@check("voice", "voice.nodes")
async def voice_nodes(ctx: Context) -> list[Result]:
    label = "Nodos de voz"
    async with ctx.db() as s:
        nodes = (await s.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.role == "voice"))).all()
    dispatch = getattr(ctx.settings, "voice_dispatch", "local")
    if not nodes:
        if dispatch == "local":
            return skipped("voice", "voice.nodes", label,
                           "VOICE_DISPATCH=local: los medios de voz corren en el mismo proceso de la API.")
        return [Result("voice", "voice.nodes", label, "fail",
                       "VOICE_DISPATCH no es local pero no hay nodos de voz activos: levanta el servicio «voice» del "
                       "docker-compose (ROLE=voice).")]
    fresh_since = utcnow() - timedelta(seconds=60)
    alive = [n for n in nodes if n.last_beat_at >= fresh_since]
    cap = sum((n.capacity or 0) for n in alive)
    load = sum((n.active_load or 0) for n in alive)
    status = "pass" if alive else "fail"
    return [Result("voice", "voice.nodes", label, status,
                   f"{len(alive)} de {len(nodes)} nodos activos; {load}/{cap} llamadas en curso." if alive else
                   "Ningún nodo de voz reporta latido en el último minuto.",
                   {"alive": len(alive), "total": len(nodes), "capacity": cap, "load": load})]
