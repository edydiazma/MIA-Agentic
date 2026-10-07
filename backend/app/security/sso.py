"""Inicio de sesión único (SSO): OIDC (código + PKCE, validación del id_token con JWKS) y SAML 2.0 (HTTP-Redirect
para la solicitud, HTTP-POST en el ACS; firma verificada con signxml y SOLO se leen datos del elemento firmado, para
evitar ataques de envoltura de firma).

El estado de la ida y vuelta viaja cifrado (crypto.seal): nadie puede leer el verificador PKCE ni alterarlo. El
regreso al panel usa un código de un solo uso de 60 s (`POST /api/auth/sso/exchange`), nunca el token en la URL.
"""

import base64
import hashlib
import re
import secrets
import zlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from lxml import etree
from signxml import XMLVerifier
from signxml.exceptions import InvalidInput, InvalidSignature
from sqlalchemy import any_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Agent, AgentGroup, AgentIdentity, Role, SSOConnection, utcnow
from app.ops import ratelimit
from app.secrets_vault import get_secret
from app.security import crypto

NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol", "saml": "urn:oasis:names:tc:SAML:2.0:assertion"}
SKEW = timedelta(minutes=2)
EMAIL_ATTRS = ("email", "mail", "emailAddress", "Email", "User.email",
               "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress")
NAME_ATTRS = ("name", "displayName", "cn", "User.FirstName", "http://schemas.microsoft.com/identity/claims/displayname")


class SSOError(Exception):
    pass


def http_client() -> httpx.AsyncClient:  # las pruebas lo reemplazan por un IdP falso
    return httpx.AsyncClient(timeout=15)


def base_url() -> str:
    return get_settings().public_base_url.rstrip("/")


def sp_entity_id(conn: SSOConnection) -> str:
    return f"{base_url()}/auth/sso/{conn.slug}/metadata"


def acs_url(conn: SSOConnection) -> str:
    return f"{base_url()}/auth/sso/{conn.slug}/acs"


def oidc_redirect_uri(conn: SSOConnection) -> str:
    return f"{base_url()}/auth/sso/{conn.slug}/callback"


async def discover(session: AsyncSession, email: str) -> SSOConnection | None:
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not domain or "@" not in email:
        return None
    return (await session.scalars(select(SSOConnection).where(
        SSOConnection.is_active, domain == any_(SSOConnection.domains)))).first()


async def enforced_for(session: AsyncSession, email: str) -> SSOConnection | None:
    conn = await discover(session, email)
    return conn if conn and conn.enforce else None


# --- OIDC -------------------------------------------------------------------------------------------------------
async def _oidc_config(conn: SSOConnection) -> dict:
    url = conn.issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with http_client() as http:
        r = await http.get(url)
    if r.status_code != 200:
        raise SSOError("No se pudo leer la configuración OIDC del proveedor")
    return r.json()


async def oidc_start(conn: SSOConnection) -> str:
    cfg = await _oidc_config(conn)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = crypto.seal({"slug": conn.slug, "nonce": nonce, "verifier": verifier}, 600)
    params = {"response_type": "code", "client_id": conn.client_id, "redirect_uri": oidc_redirect_uri(conn),
              "scope": " ".join(conn.scopes or ["openid", "email", "profile"]), "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    return cfg["authorization_endpoint"] + "?" + urlencode(params)


async def oidc_callback(session: AsyncSession, conn: SSOConnection, code: str, state: str) -> dict:
    st = crypto.unseal(state or "")
    if not st or st.get("slug") != conn.slug:
        raise SSOError("La sesión de inicio expiró: vuelve a intentarlo")
    cfg = await _oidc_config(conn)
    secret = await get_secret(session, conn.client_secret_secret_id) if conn.client_secret_secret_id else None
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": oidc_redirect_uri(conn),
            "client_id": conn.client_id, "code_verifier": st["verifier"]}
    if secret:
        data["client_secret"] = secret
    async with http_client() as http:
        tr = await http.post(cfg["token_endpoint"], data=data, headers={"Accept": "application/json"})
        if tr.status_code != 200:
            raise SSOError("El proveedor rechazó el código de inicio de sesión")
        id_token = tr.json().get("id_token")
        jr = await http.get(cfg["jwks_uri"])
    if not id_token or jr.status_code != 200:
        raise SSOError("El proveedor no devolvió un id_token válido")
    try:
        header = jwt.get_unverified_header(id_token)
        keys = jr.json().get("keys") or []
        key = next((k for k in keys if k.get("kid") == header.get("kid")), keys[0] if len(keys) == 1 else None)
        if not key or header.get("alg") in (None, "none", "HS256", "HS384", "HS512"):
            raise SSOError("Firma del id_token no admitida")
        claims = jwt.decode(id_token, jwt.PyJWK(key).key, algorithms=[header["alg"]], audience=conn.client_id,
                            issuer=cfg.get("issuer") or conn.issuer, leeway=120,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise SSOError(f"id_token inválido: {e}") from None
    if not crypto.equal(claims.get("nonce"), st["nonce"]):
        raise SSOError("id_token inválido (nonce)")
    if claims.get("email_verified") is False:
        raise SSOError("El proveedor no ha verificado tu correo")
    groups = claims.get(conn.group_claim) if conn.group_claim else None
    return {"subject": str(claims["sub"]), "email": claims.get("email"), "name": claims.get("name"),
            "groups": groups if isinstance(groups, list) else ([groups] if groups else [])}


# --- SAML -------------------------------------------------------------------------------------------------------
def _saml_time(value: str | None) -> datetime | None:
    """xs:dateTime de SAML (UTC, con o sin fracciones de segundo)."""
    if not value:
        return None
    v = re.sub(r"\.\d+", "", value.strip()).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def saml_start(conn: SSOConnection) -> str:
    if not conn.idp_sso_url:
        raise SSOError("Falta la URL de inicio de sesión del proveedor (SAML)")
    rid = "_" + secrets.token_hex(16)
    issued = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = (f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{rid}" Version="2.0" '
           f'IssueInstant="{issued}" Destination="{conn.idp_sso_url}" '
           f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" AssertionConsumerServiceURL="{acs_url(conn)}">'
           f'<saml:Issuer>{sp_entity_id(conn)}</saml:Issuer>'
           '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>'
           '</samlp:AuthnRequest>')
    deflated = zlib.compress(xml.encode())[2:-4]  # DEFLATE crudo (binding HTTP-Redirect)
    relay = crypto.seal({"slug": conn.slug, "rid": rid}, 600)
    sep = "&" if "?" in conn.idp_sso_url else "?"
    return conn.idp_sso_url + sep + urlencode({"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay})


def _pem(cert: str) -> str:
    body = "".join(line.strip() for line in cert.strip().splitlines() if "CERTIFICATE" not in line)
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"


async def saml_acs(conn: SSOConnection, saml_response: str, relay_state: str) -> dict:
    st = crypto.unseal(relay_state or "")
    if not st or st.get("slug") != conn.slug:
        raise SSOError("La sesión de inicio expiró: vuelve a intentarlo")
    if not conn.idp_certificate:
        raise SSOError("Falta el certificado del proveedor (SAML)")
    try:
        raw = base64.b64decode(saml_response, validate=False)
    except (ValueError, TypeError):
        raise SSOError("Respuesta SAML inválida") from None
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        doc = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError:
        raise SSOError("Respuesta SAML inválida") from None
    status = doc.find("samlp:Status/samlp:StatusCode", NS)
    if status is None or not status.get("Value", "").endswith(":Success"):
        raise SSOError("El proveedor no autorizó el inicio de sesión")
    try:
        # Firma de la respuesta o de la aserción; solo se confía en el elemento firmado que devuelve el verificador
        signed = XMLVerifier().verify(raw, x509_cert=_pem(conn.idp_certificate)).signed_xml
    except (InvalidSignature, InvalidInput) as e:
        raise SSOError(f"Firma SAML inválida: {e}") from None
    assertion = signed if etree.QName(signed).localname == "Assertion" else signed.find("saml:Assertion", NS)
    if assertion is None:
        raise SSOError("La respuesta SAML no trae una aserción firmada")
    now = datetime.now(UTC)
    issuer = (assertion.findtext("saml:Issuer", namespaces=NS) or "").strip()
    if conn.idp_entity_id and issuer != conn.idp_entity_id:
        raise SSOError("Emisor SAML inesperado")
    cond = assertion.find("saml:Conditions", NS)
    if cond is not None:
        nb, noa = _saml_time(cond.get("NotBefore")), _saml_time(cond.get("NotOnOrAfter"))
        if (nb and now + SKEW < nb) or (noa and now - SKEW >= noa):
            raise SSOError("La aserción SAML venció")
        audiences = [a.text.strip() for a in cond.findall("saml:AudienceRestriction/saml:Audience", NS) if a.text]
        if audiences and sp_entity_id(conn) not in audiences:
            raise SSOError("La aserción SAML no es para esta aplicación")
    conf = assertion.find("saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", NS)
    if conf is None:
        raise SSOError("Falta la confirmación del sujeto SAML")
    if conf.get("Recipient") and conf.get("Recipient") != acs_url(conn):
        raise SSOError("Destinatario SAML inválido")
    noa = _saml_time(conf.get("NotOnOrAfter"))
    if noa and now - SKEW >= noa:
        raise SSOError("La aserción SAML venció")
    if conf.get("InResponseTo") and conf.get("InResponseTo") != st.get("rid"):
        raise SSOError("La respuesta SAML no corresponde a esta solicitud")
    aid = assertion.get("ID") or ""
    if not aid or await ratelimit.hits(None, f"saml:{conn.id}:{aid}", 3600) > 1:
        raise SSOError("Aserción SAML ya utilizada")
    name_id = (assertion.findtext("saml:Subject/saml:NameID", namespaces=NS) or "").strip()
    attrs: dict[str, list[str]] = {}
    for a in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        attrs[a.get("Name", "")] = [(v.text or "").strip() for v in a.findall("saml:AttributeValue", NS)]
    email = next((attrs[k][0] for k in EMAIL_ATTRS if attrs.get(k)), None) or (name_id if "@" in name_id else None)
    name = next((attrs[k][0] for k in NAME_ATTRS if attrs.get(k)), None)
    groups = attrs.get(conn.group_claim, []) if conn.group_claim else []
    if not name_id:
        raise SSOError("La aserción SAML no trae NameID")
    return {"subject": name_id, "email": email, "name": name, "groups": groups}


def sp_metadata(conn: SSOConnection) -> str:
    return (f'<?xml version="1.0"?><md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'entityID="{sp_entity_id(conn)}"><md:SPSSODescriptor AuthnRequestsSigned="false" '
            f'WantAssertionsSigned="true" protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>'
            f'<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{acs_url(conn)}" index="0" isDefault="true"/></md:SPSSODescriptor></md:EntityDescriptor>')


async def import_idp_metadata(conn: SSOConnection) -> None:
    """Completa entityID, URL de SSO y certificado desde la metadata del IdP (botón «Probar» / al guardar)."""
    async with http_client() as http:
        r = await http.get(conn.idp_metadata_url)
    if r.status_code != 200:
        raise SSOError("No se pudo descargar la metadata del proveedor")
    md = "urn:oasis:names:tc:SAML:2.0:metadata"
    ds = "http://www.w3.org/2000/09/xmldsig#"
    doc = etree.fromstring(r.content, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    conn.idp_entity_id = doc.get("entityID") or conn.idp_entity_id
    sso = doc.find(f".//{{{md}}}IDPSSODescriptor/{{{md}}}SingleSignOnService"
                   "[@Binding='urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect']")
    if sso is not None:
        conn.idp_sso_url = sso.get("Location")
    cert = doc.find(f".//{{{md}}}IDPSSODescriptor//{{{ds}}}X509Certificate")
    if cert is not None and cert.text:
        conn.idp_certificate = cert.text.strip()


# --- Usuario: vínculo y aprovisionamiento ----------------------------------------------------------------------
async def provision(session: AsyncSession, conn: SSOConnection, ident: dict) -> Agent:
    """Busca el usuario por la identidad vinculada o por correo; si no existe y JIT está activo, lo crea."""
    link = await session.scalar(select(AgentIdentity).where(AgentIdentity.sso_connection_id == conn.id,
                                                            AgentIdentity.subject == ident["subject"]))
    agent = await session.get(Agent, link.agent_id) if link else None
    email = (ident.get("email") or "").strip().lower()
    if agent is None:
        if not email:
            raise SSOError("El proveedor no envió el correo del usuario")
        domain = email.rsplit("@", 1)[-1]
        if conn.domains and domain not in [d.lower() for d in conn.domains]:
            raise SSOError("Tu correo no pertenece a un dominio de esta empresa")
        agent = await session.scalar(select(Agent).where(Agent.organization_id == conn.organization_id,
                                                         func.lower(Agent.email) == email))
    if agent is not None and agent.organization_id != conn.organization_id:
        raise SSOError("Usuario de otra empresa")
    mapped = _mapping(conn, ident.get("groups") or [])
    if agent is None:
        if not conn.jit_provisioning:
            raise SSOError("Tu usuario no existe en la plataforma: pide al administrador que te invite")
        from app.plans import enforce_limit

        await enforce_limit(session, conn.organization_id, "users")
        role = await _role(session, conn, mapped.get("role_key"))
        agent = Agent(organization_id=conn.organization_id, email=email, name=ident.get("name") or email.split("@")[0],
                      password_hash=None, role=role.base_role if role else "agent", role_id=role.id if role else None)
        session.add(agent)
        await session.flush()
    elif mapped.get("role_key"):
        role = await _role(session, conn, mapped["role_key"])
        if role and agent.role_id != role.id:
            agent.role_id, agent.role = role.id, role.base_role
    if not agent.is_active:
        raise SSOError("Tu usuario está desactivado")
    for gid in mapped.get("group_ids", []):
        if not await session.get(AgentGroup, (agent.id, gid)):
            session.add(AgentGroup(agent_id=agent.id, group_id=gid))
    if link is None:
        session.add(AgentIdentity(agent_id=agent.id, sso_connection_id=conn.id, subject=ident["subject"],
                                  last_login_at=utcnow()))
    else:
        link.last_login_at = utcnow()
    conn.last_login_at = utcnow()
    agent.last_seen_at = utcnow()
    return agent


def _mapping(conn: SSOConnection, groups: list[str]) -> dict:
    out: dict = {"group_ids": []}
    for g in groups:
        rule = (conn.group_mapping or {}).get(g)
        if not isinstance(rule, dict):
            continue
        out["role_key"] = rule.get("role_key") or out.get("role_key")
        out["group_ids"] += [int(x) for x in rule.get("group_ids") or [] if str(x).isdigit()]
    return out


async def _role(session: AsyncSession, conn: SSOConnection, key: str | None) -> Role | None:
    if key:
        role = await session.scalar(select(Role).where(Role.organization_id == conn.organization_id, Role.key == key))
        if role:
            return role
    if conn.default_role_id:
        role = await session.get(Role, conn.default_role_id)
        if role and role.organization_id == conn.organization_id:
            return role
    return await session.scalar(select(Role).where(Role.organization_id == conn.organization_id,
                                                   Role.key == "agent", Role.is_system))


def login_code(agent: Agent) -> str:
    return crypto.seal({"agent_id": agent.id, "jti": secrets.token_hex(12)}, 60)


async def redeem_code(code: str) -> int | None:
    data = crypto.unseal(code or "")
    if not data:
        return None
    if await ratelimit.hits(None, f"sso_code:{data['jti']}", 300) > 1:
        return None  # ya usado
    return int(data["agent_id"])
