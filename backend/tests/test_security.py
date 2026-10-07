"""Seguridad de acceso (§18.4): bloqueo, límites por IP, IPs permitidas, política e historial de contraseñas,
vencimiento, 2FA (TOTP, correo, códigos de recuperación, dispositivo de confianza), recuperación de contraseña,
SSO OIDC y SAML con IdP falsos, roles y permisos, auditoría y aislamiento entre empresas."""

import base64
import datetime
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner
from sqlalchemy import select, update

from app.auth import hash_password
from app.db import SessionLocal
from app.main import app
from app.models import Agent, AuthEvent, Organization, PasswordReset, utcnow
from app.security import sso as sso_mod
from app.security import totp

ORG = 9301
OTHER = 9302
ADMIN = ("seg-admin@empresa9301.test", "Admin-Seguro-2026")
AGENT = ("seg-asesor@empresa9301.test", "Asesor-Seguro-2026")


def _anon(ip: str = "198.51.100.10") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             headers={"X-Forwarded-For": ip})


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _org(org_id: int, name: str, admin_email: str, admin_pw: str, extra: list[tuple[str, str, str]] = ()):
    async with SessionLocal() as s:
        if not await s.get(Organization, org_id):
            s.add(Organization(id=org_id, name=name, slug=f"seg-{org_id}", timezone="America/Bogota"))
            await s.flush()
        for email, pw, role in [(admin_email, admin_pw, "admin"), *extra]:
            a = await s.scalar(select(Agent).where(Agent.email == email))
            if not a:
                s.add(Agent(organization_id=org_id, email=email, name=email.split("@")[0], role=role,
                            password_hash=hash_password(pw), password_changed_at=utcnow()))
            else:
                a.password_hash, a.failed_logins, a.locked_until = hash_password(pw), 0, None
                a.mfa_method = a.mfa_secret_id = None
                a.mfa_recovery_hashes, a.must_change_password = [], False
        await s.commit()


@pytest.fixture
async def org():
    await _org(ORG, "Empresa Seguridad", *ADMIN, extra=[(*AGENT, "agent")])
    await _org(OTHER, "Otra Empresa", "seg-otra@empresa9302.test", "Otra-Clave-2026")
    async with _anon() as c:
        tok = (await c.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})).json()["access_token"]
        c.headers.update(_auth(tok))
        await c.put("/api/security/policy", json={"allowed_ips": [], "mfa_required": "none", "expiry_days": 0})
        yield c


async def _login(c: httpx.AsyncClient, email: str, password: str, **extra) -> httpx.Response:
    return await c.post("/api/auth/login", json={"email": email, "password": password, **extra})


async def _events(email: str) -> list[str]:
    async with SessionLocal() as s:
        return list((await s.scalars(select(AuthEvent.event).where(AuthEvent.email == email)
                                     .order_by(AuthEvent.id))).all())


# --- Bloqueo, límite por IP, IPs permitidas ----------------------------------------------------------------
async def test_lockout_unlock_and_audit(org):
    async with _anon("198.51.100.21") as c:
        for _ in range(5):
            r = await _login(c, AGENT[0], "mala-clave")
            assert r.status_code == 401 and r.json()["detail"] == "Correo o contraseña incorrectos"
        r = await _login(c, AGENT[0], AGENT[1])  # la correcta ya no entra: cuenta bloqueada
        assert r.status_code == 423
    async with SessionLocal() as s:
        a = await s.scalar(select(Agent).where(Agent.email == AGENT[0]))
    assert (await org.post(f"/api/security/users/{a.id}/unlock")).status_code == 200
    async with _anon("198.51.100.21") as c:
        assert (await _login(c, AGENT[0], AGENT[1])).status_code == 200
    ev = await _events(AGENT[0])
    assert "login_failed" in ev and "locked" in ev and "unlocked" in ev and ev[-1] == "login_ok"
    audit = (await org.get("/api/security/audit", params={"email": AGENT[0]})).json()
    assert {e["event"] for e in audit} >= {"login_failed", "locked", "login_ok"}


async def test_ip_rate_limit_on_failures(org):
    async with _anon("203.0.113.77") as c:
        for i in range(30):
            await _login(c, f"nadie{i}@x.test", "x")
        assert (await _login(c, ADMIN[0], ADMIN[1])).status_code == 429
    async with _anon("203.0.113.78") as c:  # otra IP no se afecta
        assert (await _login(c, ADMIN[0], ADMIN[1])).status_code == 200


async def test_ip_allowlist(org):
    # No deja guardar una lista que dejaría fuera a quien la configura
    assert (await org.put("/api/security/policy", json={"allowed_ips": ["192.0.2.0/24"]})).status_code == 422
    async with _anon("192.0.2.10") as c:
        tok = (await _login(c, ADMIN[0], ADMIN[1])).json()["access_token"]
        r = await c.put("/api/security/policy", json={"allowed_ips": ["192.0.2.0/24", "bad"]}, headers=_auth(tok))
        assert r.status_code == 422
        r = await c.put("/api/security/policy", json={"allowed_ips": ["192.0.2.0/24"]}, headers=_auth(tok))
        assert r.status_code == 200 and r.json()["allowed_ips"] == ["192.0.2.0/24"]
        assert (await _login(c, AGENT[0], AGENT[1])).status_code == 200
    async with _anon("198.51.100.99") as c:
        r = await _login(c, AGENT[0], AGENT[1])
        assert r.status_code == 403
    async with _anon("192.0.2.10") as c:
        tok = (await _login(c, ADMIN[0], ADMIN[1])).json()["access_token"]
        await c.put("/api/security/policy", json={"allowed_ips": []}, headers=_auth(tok))
    assert "ip_blocked" in await _events(AGENT[0])
    # La política no se expone por el endpoint genérico de ajustes
    assert (await org.get("/api/settings/security")).status_code == 404


# --- Contraseñas -------------------------------------------------------------------------------------------
async def test_password_policy_history_and_expiry(org):
    async with _anon() as c:
        tok = (await _login(c, AGENT[0], AGENT[1])).json()["access_token"]
        c.headers.update(_auth(tok))
        r = await c.post("/api/auth/password", json={"current_password": AGENT[1], "new_password": "corta"})
        assert r.status_code == 422 and len(r.json()["detail"]["errors"]) >= 2
        r = await c.post("/api/auth/password", json={"current_password": "otra", "new_password": "Nueva-Clave-2026"})
        assert r.status_code == 422  # contraseña actual incorrecta
        assert (await c.post("/api/auth/password", json={"current_password": AGENT[1],
                                                         "new_password": "Nueva-Clave-2026"})).status_code == 200
        r = await c.post("/api/auth/password", json={"current_password": "Nueva-Clave-2026", "new_password": AGENT[1]})
        assert r.status_code == 422 and "últimas" in r.json()["detail"]["errors"][0]  # historial
    # Vencimiento: 30 días de vigencia y la contraseña tiene 40 → debe cambiarla
    await org.put("/api/security/policy", json={"expiry_days": 30})
    async with SessionLocal() as s:
        await s.execute(update(Agent).where(Agent.email == AGENT[0])
                        .values(password_changed_at=utcnow() - timedelta(days=40)))
        await s.commit()
    async with _anon() as c:
        r = await _login(c, AGENT[0], "Nueva-Clave-2026")
        assert r.status_code == 200 and r.json()["must_change_password"] is True
        me = (await c.get("/api/auth/me", headers=_auth(r.json()["access_token"]))).json()
        assert me["must_change_password"] is True
    await org.put("/api/security/policy", json={"expiry_days": 0})
    # Crear usuarios también exige la política
    r = await org.post("/api/agents", json={"email": "debil@empresa9301.test", "name": "Débil", "password": "12345678"})
    assert r.status_code == 422


# --- Segundo factor ------------------------------------------------------------------------------------------
async def test_totp_recovery_codes_and_trusted_device(org):
    async with _anon("198.51.100.30") as c:
        tok = (await _login(c, AGENT[0], AGENT[1])).json()["access_token"]
        c.headers.update(_auth(tok))
        setup = (await c.post("/api/auth/mfa/totp/setup")).json()
        assert setup["otpauth_uri"].startswith("otpauth://totp/")
        assert (await c.post("/api/auth/mfa/totp/enable", json={"setup_token": setup["setup_token"],
                                                               "code": "000000"})).status_code == 422
        r = await c.post("/api/auth/mfa/totp/enable", json={"setup_token": setup["setup_token"],
                                                           "code": totp.now_code(setup["secret"])})
        codes = r.json()["recovery_codes"]
        assert r.status_code == 200 and len(codes) == 10
        assert (await c.get("/api/auth/me")).json()["mfa"]["enabled"] is True

    async with _anon("198.51.100.30") as c:
        r = (await _login(c, AGENT[0], AGENT[1])).json()
        assert r["mfa_required"] is True and "access_token" not in r and r["methods"][0] == "totp"
        bad = await c.post("/api/auth/mfa/verify", json={"mfa_token": r["mfa_token"], "code": "123456"})
        assert bad.status_code == 401
        ok = await c.post("/api/auth/mfa/verify", json={"mfa_token": r["mfa_token"],
                                                       "code": totp.now_code(setup["secret"]), "remember_device": True})
        assert ok.status_code == 200 and ok.json()["access_token"] and ok.json()["device_token"]
        device = ok.json()["device_token"]
        # Reto ya usado
        assert (await c.post("/api/auth/mfa/verify", json={"mfa_token": r["mfa_token"],
                                                           "code": totp.now_code(setup["secret"])})).status_code == 401
        # Dispositivo de confianza, misma IP: entra sin código
        r2 = (await _login(c, AGENT[0], AGENT[1], device_token=device)).json()
        assert "access_token" in r2
    async with _anon("198.51.100.31") as c:  # otra IP: vuelve a pedir el código; recuperación de un solo uso
        r3 = (await _login(c, AGENT[0], AGENT[1], device_token=device)).json()
        assert r3["mfa_required"] is True
        ok = await c.post("/api/auth/mfa/verify", json={"mfa_token": r3["mfa_token"], "code": codes[0]})
        assert ok.status_code == 200 and ok.json()["recovery_codes_left"] == 9
        r4 = (await _login(c, AGENT[0], AGENT[1])).json()
        assert (await c.post("/api/auth/mfa/verify", json={"mfa_token": r4["mfa_token"],
                                                           "code": codes[0]})).status_code == 401
    # El administrador puede restablecer el 2FA de un usuario
    async with SessionLocal() as s:
        a = await s.scalar(select(Agent).where(Agent.email == AGENT[0]))
    assert (await org.post(f"/api/security/users/{a.id}/mfa/reset")).status_code == 200
    async with _anon() as c:
        assert "access_token" in (await _login(c, AGENT[0], AGENT[1])).json()
    assert {"mfa_enabled", "mfa_challenge", "mfa_failed", "mfa_ok", "mfa_disabled"} <= set(await _events(AGENT[0]))


async def test_mandatory_email_mfa(org, monkeypatch):
    sent: list[tuple[str, str]] = []

    async def fake_send(to, subject, text):
        sent.append((to, text))
        return True

    monkeypatch.setattr("app.onboarding.invites.send_email", fake_send)
    await org.put("/api/security/policy", json={"mfa_required": "all"})
    async with _anon() as c:
        r = (await _login(c, AGENT[0], AGENT[1])).json()
        assert r["mfa_required"] is True and r["methods"] == ["email"] and r["email_hint"].startswith("se***@")
        code = sent[-1][1].split("es ")[1][:6]
        ok = await c.post("/api/auth/mfa/verify", json={"mfa_token": r["mfa_token"], "code": code})
        assert ok.status_code == 200
        me = (await c.get("/api/auth/me", headers=_auth(ok.json()["access_token"]))).json()
        assert me["mfa"]["required"] is True
        # 2FA exigido: no se puede desactivar
        r = await c.post("/api/auth/mfa/disable", json={"password": AGENT[1]},
                         headers=_auth(ok.json()["access_token"]))
        assert r.status_code == 409

    async def no_smtp(to, subject, text):
        return False

    monkeypatch.setattr("app.onboarding.invites.send_email", no_smtp)
    async with _anon() as c:  # sin correo ni app: entra, pero el panel le exige activar 2FA
        r = (await _login(c, AGENT[0], AGENT[1])).json()
        assert r["access_token"] and r["mfa_enrollment_required"] is True
    await org.put("/api/security/policy", json={"mfa_required": "none"})


# --- Recuperación ---------------------------------------------------------------------------------------------
async def test_forgot_and_reset_single_use(org, monkeypatch):
    sent: list[str] = []

    async def fake_send(to, subject, text):
        sent.append(text)
        return True

    monkeypatch.setattr("app.onboarding.invites.send_email", fake_send)
    async with _anon("198.51.100.40") as c:
        assert (await c.post("/api/auth/forgot", json={"email": "nadie@nada.test"})).json() == {"ok": True}
        assert not sent
        assert (await c.post("/api/auth/forgot", json={"email": AGENT[0].upper()})).json() == {"ok": True}
        token = sent[-1].split("/recuperar/")[1].split()[0]
        info = await c.get(f"/api/auth/reset/{token}")
        assert info.status_code == 200 and info.json()["email"].startswith("se***")
        assert (await c.post("/api/auth/reset", json={"token": token, "password": "debil"})).status_code == 422
        assert (await c.post("/api/auth/reset", json={"token": token, "password": "Recuperada-2026"})).status_code == 200
        assert (await c.post("/api/auth/reset", json={"token": token, "password": "Otra-Vez-2026x"})).status_code == 410
        assert (await _login(c, AGENT[0], "Recuperada-2026")).status_code == 200
        # Enlace vencido
        await c.post("/api/auth/forgot", json={"email": AGENT[0]})
        token2 = sent[-1].split("/recuperar/")[1].split()[0]
    async with SessionLocal() as s:
        await s.execute(update(PasswordReset).values(expires_at=utcnow() - timedelta(minutes=1)))
        await s.commit()
    async with _anon() as c:
        assert (await c.post("/api/auth/reset", json={"token": token2, "password": "Vencida-2026xx"})).status_code == 410
    assert "password_reset" in await _events(AGENT[0])


# --- SSO -------------------------------------------------------------------------------------------------------
class FakeIdP:
    """IdP OIDC falso: discovery, token (devuelve id_token RS256) y JWKS."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.nonce = None
        self.claims: dict = {}
        self.issuer = "https://idp.acme.test"

    def client(self):
        idp = self

        class C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                if url.endswith("/.well-known/openid-configuration"):
                    return httpx.Response(200, json={"issuer": idp.issuer,
                                                     "authorization_endpoint": idp.issuer + "/authorize",
                                                     "token_endpoint": idp.issuer + "/token",
                                                     "jwks_uri": idp.issuer + "/jwks"})
                if url.endswith("/jwks"):
                    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(idp.key.public_key(), as_dict=True)
                    return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "alg": "RS256", "use": "sig"}]})
                return httpx.Response(404)

            async def post(self, url, data=None, **kw):
                assert data["code_verifier"] and data["code"] == "CODE-OK"
                now = int(time.time())
                token = jwt.encode({"iss": idp.issuer, "aud": "panel-client", "sub": "idp-user-1", "iat": now,
                                    "exp": now + 300, "nonce": idp.nonce, **idp.claims}, idp.key, algorithm="RS256",
                                   headers={"kid": "k1"})
                return httpx.Response(200, json={"id_token": token, "access_token": "x"})

        return C()


async def test_oidc_sso_jit_mapping_and_enforce(org, monkeypatch):
    idp = FakeIdP()
    monkeypatch.setattr(sso_mod, "http_client", idp.client)
    r = await org.post("/api/security/sso", json={
        "protocol": "oidc", "name": "OneLogin", "slug": "acme-oidc", "issuer": idp.issuer, "client_id": "panel-client",
        "client_secret": "s3cr3t", "domains": ["Acme.test"], "jit_provisioning": True, "group_claim": "groups",
        "group_mapping": {"Supervisores": {"role_key": "supervisor"}}, "is_active": True})
    assert r.status_code == 200, r.text
    conn = r.json()
    assert conn["domains"] == ["acme.test"] and conn["has_client_secret"] and "client_secret" not in conn
    async with _anon() as c:
        d = (await c.get("/api/auth/sso/discover", params={"email": "ana@acme.test"})).json()
        assert d["sso"]["slug"] == "acme-oidc"
        start = await c.get("/auth/sso/acme-oidc/start", follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["panel-client"]
        idp.nonce = q["nonce"][0]
        idp.claims = {"email": "ana@acme.test", "name": "Ana Acme", "email_verified": True, "groups": ["Supervisores"]}
        cb = await c.get("/auth/sso/acme-oidc/callback", params={"code": "CODE-OK", "state": q["state"][0]},
                         follow_redirects=False)
        loc = urlparse(cb.headers["location"])
        assert loc.path == "/sso/callback", cb.headers["location"]
        code = parse_qs(loc.query)["code"][0]
        ex = await c.post("/api/auth/sso/exchange", json={"code": code})
        assert ex.status_code == 200
        me = (await c.get("/api/auth/me", headers=_auth(ex.json()["access_token"]))).json()
        assert me["email"] == "ana@acme.test" and me["role_info"]["key"] == "supervisor"
        assert (await c.post("/api/auth/sso/exchange", json={"code": code})).status_code == 401  # un solo uso
        # Estado alterado o nonce distinto → error
        bad = await c.get("/auth/sso/acme-oidc/callback", params={"code": "CODE-OK", "state": "manipulado"},
                          follow_redirects=False)
        assert "sso_error" in bad.headers["location"]
        idp.nonce = "otro"
        bad = await c.get("/auth/sso/acme-oidc/callback", params={"code": "CODE-OK", "state": q["state"][0]},
                          follow_redirects=False)
        assert "sso_error" in bad.headers["location"]
    # Exigir SSO: la contraseña local queda bloqueada para ese dominio
    await org.put(f"/api/security/sso/{conn['id']}", json={**{k: conn[k] for k in (
        "protocol", "name", "issuer", "client_id", "domains", "jit_provisioning", "group_claim", "group_mapping")},
        "enforce": True, "is_active": True})
    async with SessionLocal() as s:
        await s.execute(update(Agent).where(Agent.email == "ana@acme.test").values(password_hash=hash_password("x")))
        await s.commit()
    async with _anon() as c:
        r = await _login(c, "ana@acme.test", "x")
        assert r.status_code == 403 and r.json()["detail"]["sso"]["slug"] == "acme-oidc"
    await org.delete(f"/api/security/sso/{conn['id']}")
    assert "sso_login" in await _events("ana@acme.test")


def _saml_keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.saml.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(7).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()), cert.public_bytes(serialization.Encoding.PEM))


def _saml_response(key, cert, *, aid, rid, audience, recipient, email, sign=True, minutes=5):
    a, p = "urn:oasis:names:tc:SAML:2.0:assertion", "urn:oasis:names:tc:SAML:2.0:protocol"
    now = datetime.datetime.now(datetime.UTC)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    later = (now + timedelta(minutes=minutes)).strftime(fmt)
    assertion = etree.fromstring(
        f'<saml:Assertion xmlns:saml="{a}" ID="{aid}" Version="2.0" IssueInstant="{now.strftime(fmt)}">'
        '<saml:Issuer>https://idp.saml.test</saml:Issuer>'
        f'<saml:Subject><saml:NameID>{email}</saml:NameID><saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData InResponseTo="{rid}" Recipient="{recipient}" NotOnOrAfter="{later}"/>'
        '</saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{(now - timedelta(minutes=1)).strftime(fmt)}" NotOnOrAfter="{later}">'
        f'<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
        '<saml:AttributeStatement><saml:Attribute Name="displayName"><saml:AttributeValue>Beto Saml</saml:AttributeValue>'
        '</saml:Attribute></saml:AttributeStatement></saml:Assertion>')
    if sign:
        assertion = XMLSigner(signature_algorithm="rsa-sha256", digest_algorithm="sha256",
                              c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#").sign(
            assertion, key=key, cert=cert, id_attribute="ID", reference_uri=aid)
    resp = etree.fromstring(f'<samlp:Response xmlns:samlp="{p}" ID="_r{aid}" Version="2.0" InResponseTo="{rid}">'
                            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
                            '</samlp:Status></samlp:Response>')
    resp.append(assertion)
    return base64.b64encode(etree.tostring(resp)).decode()


async def test_saml_sso_signature_audience_replay(org):
    key, cert = _saml_keys()
    r = await org.post("/api/security/sso", json={
        "protocol": "saml", "name": "Okta", "slug": "acme-saml", "idp_entity_id": "https://idp.saml.test",
        "idp_sso_url": "https://idp.saml.test/sso", "idp_certificate": cert.decode(), "domains": ["saml.acme.test"],
        "jit_provisioning": True, "is_active": True})
    assert r.status_code == 200, r.text
    sp, sso_id = r.json()["sp"], r.json()["id"]
    async with _anon() as c:
        md = await c.get("/auth/sso/acme-saml/metadata")
        assert md.status_code == 200 and sp["acs_url"] in md.text
        start = await c.get("/auth/sso/acme-saml/start", follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        relay = q["RelayState"][0]
        import zlib

        req = zlib.decompress(base64.b64decode(q["SAMLRequest"][0]), -15)
        rid = etree.fromstring(req).get("ID")
        good = dict(rid=rid, audience=sp["entity_id"], recipient=sp["acs_url"], email="beto@saml.acme.test")
        resp = _saml_response(key, cert, aid="_a100", **good)
        acs = await c.post("/auth/sso/acme-saml/acs", data={"SAMLResponse": resp, "RelayState": relay},
                           follow_redirects=False)
        loc = urlparse(acs.headers["location"])
        assert acs.status_code == 303 and loc.path == "/sso/callback", acs.headers["location"]
        ex = await c.post("/api/auth/sso/exchange", json={"code": parse_qs(loc.query)["code"][0]})
        me = (await c.get("/api/auth/me", headers=_auth(ex.json()["access_token"]))).json()
        assert me["email"] == "beto@saml.acme.test" and me["name"] == "Beto Saml"
        # Reenvío de la misma aserción, aserción sin firma, audiencia ajena, firma de otra llave → rechazadas
        for bad in (resp, _saml_response(key, cert, aid="_a101", sign=False, **good),
                    _saml_response(key, cert, aid="_a102", **{**good, "audience": "https://otra.app"}),
                    _saml_response(_saml_keys()[0], cert, aid="_a103", **good)):
            r = await c.post("/auth/sso/acme-saml/acs", data={"SAMLResponse": bad, "RelayState": relay},
                             follow_redirects=False)
            assert "sso_error" in r.headers["location"], r.headers["location"]
    await org.delete(f"/api/security/sso/{sso_id}")


# --- Roles y permisos -------------------------------------------------------------------------------------------
async def test_custom_roles_and_permissions(org):
    cat = (await org.get("/api/roles/catalog")).json()
    assert any(g["group"] == "Datos sensibles" for g in cat["groups"])
    r = await org.post("/api/roles", json={"name": "Analista de datos", "base_role": "agent", "data_scope": "all",
                                           "permissions": ["contacts.view", "exports.contacts", "reports.view_all"]})
    assert r.status_code == 200, r.text
    role = r.json()
    assert (await org.post("/api/roles", json={"name": "X", "permissions": ["no.existe"]})).status_code == 422
    async with SessionLocal() as s:
        agent = await s.scalar(select(Agent).where(Agent.email == AGENT[0]))
    assert (await org.put(f"/api/roles/assign/{agent.id}", json={"role_id": role["id"]})).status_code == 200
    async with _anon() as c:
        tok = (await _login(c, AGENT[0], AGENT[1])).json()["access_token"]
        c.headers.update(_auth(tok))
        me = (await c.get("/api/auth/me")).json()
        assert me["role_info"]["key"] == role["key"] and "exports.contacts" in me["permissions"]
        assert (await c.get("/api/contacts/export.csv")).status_code == 200
        assert (await c.get("/api/security/policy")).status_code == 403
        assert (await c.get("/api/security/audit")).status_code == 403
        assert (await c.post("/api/roles", json={"name": "Yo admin", "base_role": "admin"})).status_code == 403
    # Rol del sistema «Asesor»: sin exportación
    sys_agent = (await org.get("/api/roles")).json()
    agent_role = next(x for x in sys_agent if x["key"] == "agent")
    assert agent_role["is_system"] and "exports.contacts" not in agent_role["permissions"]
    await org.put(f"/api/roles/assign/{agent.id}", json={"role_id": agent_role["id"]})
    async with _anon() as c:
        tok = (await _login(c, AGENT[0], AGENT[1])).json()["access_token"]
        assert (await c.get("/api/contacts/export.csv", headers=_auth(tok))).status_code == 403
    # El rol Administrador del sistema no se edita; los del sistema no se borran
    admin_role = next(x for x in sys_agent if x["key"] == "admin")
    assert admin_role["locked"] and (await org.put(f"/api/roles/{admin_role['id']}", json={
        "name": "Admin", "base_role": "admin", "permissions": []})).status_code == 403
    assert (await org.delete(f"/api/roles/{agent_role['id']}")).status_code == 403
    dup = (await org.post(f"/api/roles/{role['id']}/duplicate")).json()
    assert dup["permissions"] == role["permissions"] and dup["name"].startswith("Analista de datos (copia")
    assert (await org.delete(f"/api/roles/{dup['id']}")).status_code == 200


async def test_org_isolation(org):
    async with _anon() as c:
        tok = (await _login(c, "seg-otra@empresa9302.test", "Otra-Clave-2026")).json()["access_token"]
        c.headers.update(_auth(tok))
        assert all(e["email"] in (None, "seg-otra@empresa9302.test") or not e["email"].endswith("empresa9301.test")
                   for e in (await c.get("/api/security/audit")).json())
        mine = {r["id"] for r in (await c.get("/api/roles")).json()}
        theirs = {r["id"] for r in (await org.get("/api/roles")).json()}
        assert mine and not mine & theirs
        async with SessionLocal() as s:
            agent = await s.scalar(select(Agent).where(Agent.email == AGENT[0]))
        assert (await c.post(f"/api/security/users/{agent.id}/unlock")).status_code == 404
        assert (await c.put(f"/api/roles/assign/{agent.id}", json={"role_id": next(iter(mine))})).status_code == 404
