"""Calendarios: Google Calendar (API v3) y Microsoft 365 / Outlook (Graph).

- Las citas del panel/bot/flujos se publican como eventos (crear, mover, cancelar) en el calendario del asesor
  (settings.calendars.agents[agent_id]) o en el calendario por defecto de la conexión.
- Dos vías: si el evento se cancela o se mueve en el calendario, la cita cambia igual (pull incremental con
  syncToken / deltaLink).
- Disponibilidad: los ocupados del calendario (free/busy, getSchedule) bloquean horarios de citas, sin contar los
  eventos que creamos nosotros.
"""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.hub import common, http
from app.hub.http import HubError
from app.models import Appointment, Contact, IntegrationConnection, utcnow

log = logging.getLogger(__name__)
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.readonly"]
GRAPH = "https://graph.microsoft.com/v1.0"
MS_SCOPES = ["offline_access", "User.Read", "Calendars.ReadWrite"]
MARK = "wa_agent_appointment_id"  # propiedad privada del evento: identifica nuestras citas


def _ms_base() -> str:
    return f"https://login.microsoftonline.com/{get_settings().microsoft_tenant or 'common'}/oauth2/v2.0"


def configured(provider: str) -> bool:
    s = get_settings()
    if provider == "google_calendar":
        return bool(s.google_oauth_client_id and s.google_oauth_client_secret)
    return bool(s.microsoft_client_id and s.microsoft_client_secret)


def redirect_uri(provider: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/api/hub/oauth/{provider}/callback"


def authorize_url(provider: str, state: str) -> str:
    s = get_settings()
    if provider == "google_calendar":
        return GOOGLE_AUTH + "?" + urlencode({"client_id": s.google_oauth_client_id, "redirect_uri": redirect_uri(provider),
                                              "response_type": "code", "scope": " ".join(GOOGLE_SCOPES),
                                              "access_type": "offline", "prompt": "consent", "state": state})
    return f"{_ms_base()}/authorize?" + urlencode({"client_id": s.microsoft_client_id, "response_type": "code",
                                                  "redirect_uri": redirect_uri(provider), "response_mode": "query",
                                                  "scope": " ".join(MS_SCOPES), "state": state})


async def exchange_code(provider: str, code: str) -> dict:
    s = get_settings()
    if provider == "google_calendar":
        data = {"code": code, "client_id": s.google_oauth_client_id, "client_secret": s.google_oauth_client_secret,
                "redirect_uri": redirect_uri(provider), "grant_type": "authorization_code"}
        url = GOOGLE_TOKEN
    else:
        data = {"code": code, "client_id": s.microsoft_client_id, "client_secret": s.microsoft_client_secret,
                "redirect_uri": redirect_uri(provider), "grant_type": "authorization_code", "scope": " ".join(MS_SCOPES)}
        url = f"{_ms_base()}/token"
    return http.check(await http.send("POST", url, data=data), "Calendario").json()


async def _refresh(session: AsyncSession, conn: IntegrationConnection) -> str:
    s = get_settings()
    refresh = await common.secret(session, conn, "refresh_token")
    if not refresh:
        raise HubError("El calendario no tiene refresh token: vuelve a conectarlo", retryable=False, status=401)
    if conn.provider == "google_calendar":
        data = {"client_id": s.google_oauth_client_id, "client_secret": s.google_oauth_client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token"}
        url = GOOGLE_TOKEN
    else:
        data = {"client_id": s.microsoft_client_id, "client_secret": s.microsoft_client_secret, "refresh_token": refresh,
                "grant_type": "refresh_token", "scope": " ".join(MS_SCOPES)}
        url = f"{_ms_base()}/token"
    tokens = http.check(await http.send("POST", url, data=data), "Calendario").json()
    await save_tokens(session, conn, tokens)
    return tokens["access_token"]


async def save_tokens(session: AsyncSession, conn: IntegrationConnection, tokens: dict) -> None:
    from app.secrets_vault import put_secret

    if tokens.get("access_token"):
        conn.access_token_secret_id = await put_secret(session, tokens["access_token"], f"integration:{conn.id}:access",
                                                       conn.access_token_secret_id)
    if tokens.get("refresh_token"):
        conn.refresh_token_secret_id = await put_secret(session, tokens["refresh_token"],
                                                        f"integration:{conn.id}:refresh", conn.refresh_token_secret_id)
    if tokens.get("expires_in"):
        conn.expires_at = utcnow() + timedelta(seconds=int(tokens["expires_in"]))


async def token(session: AsyncSession, conn: IntegrationConnection) -> str:
    if conn.expires_at and conn.expires_at - utcnow() < timedelta(minutes=2):
        return await _refresh(session, conn)
    tok = await common.secret(session, conn, "access_token")
    if not tok:
        raise HubError("El calendario no tiene token", retryable=False, status=401)
    return tok


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


# --- Clientes ------------------------------------------------------------------------------------------------
class Google:
    def __init__(self, tok: str):
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    async def _call(self, method: str, path: str, body=None, params=None):
        return http.check(await http.send(method, f"{GOOGLE_API}{path}", headers=self.h, json_body=body, params=params),
                          "Google Calendar")

    def _event(self, a: Appointment, summary: str, description: str) -> dict:
        end = a.starts_at + timedelta(minutes=a.duration_min or 30)
        return {"summary": summary, "description": description, "start": {"dateTime": _iso(a.starts_at)},
                "end": {"dateTime": _iso(end)}, "extendedProperties": {"private": {MARK: str(a.id)}}}

    async def upsert(self, cal: str, a: Appointment, summary: str, description: str) -> str:
        if a.external_event_id:
            r = await self._call("PATCH", f"/calendars/{cal}/events/{a.external_event_id}",
                                 self._event(a, summary, description))
        else:
            r = await self._call("POST", f"/calendars/{cal}/events", self._event(a, summary, description))
        return r.json()["id"]

    async def delete(self, cal: str, event_id: str) -> None:
        try:
            await self._call("DELETE", f"/calendars/{cal}/events/{event_id}")
        except HubError as e:
            if e.status not in (404, 410):
                raise

    async def busy(self, cals: list[str], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        r = await self._call("POST", "/freeBusy", {"timeMin": _iso(start), "timeMax": _iso(end),
                                                   "items": [{"id": c} for c in cals]})
        out = []
        for data in (r.json().get("calendars") or {}).values():
            for b in data.get("busy") or []:
                out.append((common.parse_dt(b["start"]), common.parse_dt(b["end"])))
        return out

    async def changes(self, cal: str, sync_token: str | None) -> tuple[list[dict], str | None]:
        """Eventos cambiados (incl. cancelados). Sin token: desde hace 1 día (solo para obtener el token)."""
        items, page = [], None
        params = {"showDeleted": "true", "singleEvents": "true", "maxResults": 250}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = _iso(utcnow() - timedelta(days=1))
        for _ in range(20):
            if page:
                params["pageToken"] = page
            try:
                data = (await self._call("GET", f"/calendars/{cal}/events", params=params)).json()
            except HubError as e:
                if e.status == 410:  # token vencido: sincronización completa
                    return await self.changes(cal, None)
                raise
            for ev in data.get("items") or []:
                mark = ((ev.get("extendedProperties") or {}).get("private") or {}).get(MARK)
                items.append({"id": ev["id"], "appointment_id": mark, "cancelled": ev.get("status") == "cancelled",
                              "start": common.parse_dt((ev.get("start") or {}).get("dateTime"))})
            page = data.get("nextPageToken")
            if not page:
                return items, data.get("nextSyncToken")
        return items, None


class Outlook:
    def __init__(self, tok: str):
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                  "Prefer": 'outlook.timezone="UTC"'}

    @staticmethod
    def _root(cal: str) -> str:
        return "/me" if cal in ("", "primary", "me") else f"/users/{cal}"

    async def _call(self, method: str, path: str, body=None, params=None, url: str | None = None):
        return http.check(await http.send(method, url or f"{GRAPH}{path}", headers=self.h, json_body=body, params=params),
                          "Outlook")

    def _event(self, a: Appointment, summary: str, description: str) -> dict:
        end = a.starts_at + timedelta(minutes=a.duration_min or 30)
        fmt = "%Y-%m-%dT%H:%M:%S"
        return {"subject": summary, "body": {"contentType": "text", "content": description},
                "start": {"dateTime": a.starts_at.astimezone(UTC).strftime(fmt), "timeZone": "UTC"},
                "end": {"dateTime": end.astimezone(UTC).strftime(fmt), "timeZone": "UTC"},
                "transactionId": f"wa-agent-{a.id}", "categories": ["WA Agent"],
                "singleValueExtendedProperties": [{"id": "String {66f5a359-4659-4830-9070-00047ec6ac6e} Name " + MARK,
                                                   "value": str(a.id)}]}

    async def upsert(self, cal: str, a: Appointment, summary: str, description: str) -> str:
        root = self._root(cal)
        if a.external_event_id:
            body = self._event(a, summary, description)
            body.pop("transactionId", None)
            r = await self._call("PATCH", f"{root}/events/{a.external_event_id}", body)
        else:
            r = await self._call("POST", f"{root}/events", self._event(a, summary, description))
        return r.json()["id"]

    async def delete(self, cal: str, event_id: str) -> None:
        try:
            await self._call("DELETE", f"{self._root(cal)}/events/{event_id}")
        except HubError as e:
            if e.status != 404:
                raise

    async def busy(self, cals: list[str], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        emails = [c for c in cals if "@" in c]
        if not emails:
            me = (await self._call("GET", "/me", params={"$select": "mail,userPrincipalName"})).json()
            emails = [me.get("mail") or me.get("userPrincipalName")]
        fmt = "%Y-%m-%dT%H:%M:%S"
        r = await self._call("POST", "/me/calendar/getSchedule", {
            "schedules": emails, "startTime": {"dateTime": start.astimezone(UTC).strftime(fmt), "timeZone": "UTC"},
            "endTime": {"dateTime": end.astimezone(UTC).strftime(fmt), "timeZone": "UTC"},
            "availabilityViewInterval": 15})
        out = []
        for sched in r.json().get("value") or []:
            for it in sched.get("scheduleItems") or []:
                if it.get("status") in ("busy", "oof", "tentative"):
                    s = common.parse_dt((it.get("start") or {}).get("dateTime"))
                    e = common.parse_dt((it.get("end") or {}).get("dateTime"))
                    if s and e:
                        out.append((s, e))
        return out

    async def changes(self, cal: str, delta_link: str | None) -> tuple[list[dict], str | None]:
        root = self._root(cal)
        url = delta_link
        params = None
        if not url:
            params = {"startDateTime": _iso(utcnow() - timedelta(days=1)), "endDateTime": _iso(utcnow() + timedelta(days=365))}
        items = []
        for _ in range(20):
            data = (await self._call("GET", f"{root}/calendarView/delta", params=params, url=url)).json()
            for ev in data.get("value") or []:
                removed = "@removed" in ev or ev.get("isCancelled")
                tx = ev.get("transactionId") or ""
                items.append({"id": ev["id"], "appointment_id": tx.removeprefix("wa-agent-") if tx.startswith("wa-agent-") else None,
                              "cancelled": bool(removed),
                              "start": common.parse_dt(((ev.get("start") or {}).get("dateTime") or "") + "Z")
                              if (ev.get("start") or {}).get("dateTime") else None})
            if data.get("@odata.nextLink"):
                url, params = data["@odata.nextLink"], None
                continue
            return items, data.get("@odata.deltaLink")
        return items, None


async def client_for(session: AsyncSession, conn: IntegrationConnection):
    tok = await token(session, conn)
    return Google(tok) if conn.provider == "google_calendar" else Outlook(tok)


# --- Citas ↔ eventos -------------------------------------------------------------------------------------------
def calendar_for(conn: IntegrationConnection, agent_id: int | None) -> str:
    cals = (conn.settings or {}).get("calendars") or {}
    if agent_id is not None and str(agent_id) in (cals.get("agents") or {}):
        return cals["agents"][str(agent_id)]
    return cals.get("default") or "primary"


async def org_calendar(session: AsyncSession, org: int) -> IntegrationConnection | None:
    return (await session.scalars(select(IntegrationConnection).where(
        IntegrationConnection.organization_id == org, IntegrationConnection.provider.in_(common.CALENDARS),
        IntegrationConnection.status == "connected").order_by(IntegrationConnection.id).limit(1))).first()


async def sync_appointment(session: AsyncSession, appt: Appointment, conn: IntegrationConnection | None = None) -> str:
    """Publica la cita en el calendario (crear / mover / cancelar). Devuelve la acción."""
    conn = conn or (await session.get(IntegrationConnection, appt.calendar_connection_id)
                    if appt.calendar_connection_id else await org_calendar(session, appt.organization_id))
    if not conn or conn.status != "connected" or not (conn.settings or {}).get("push_appointments", True):
        return "skipped"
    client = await client_for(session, conn)
    cal = calendar_for(conn, appt.agent_id)
    action = "skipped"
    if appt.status in ("cancelled", "no_show"):
        if appt.external_event_id and appt.status == "cancelled":
            await client.delete(cal, appt.external_event_id)
            action = "deleted"
    else:
        contact = await session.get(Contact, appt.contact_id)
        who = (contact.name or (f"+{contact.wa_id}" if contact.wa_id else "Cliente")) if contact else "Cliente"
        description = "\n".join(x for x in (appt.notes, f"Cliente: {who}",
                                             f"WhatsApp: +{contact.wa_id}" if contact and contact.wa_id else None) if x)
        action = "updated" if appt.external_event_id else "created"
        appt.external_event_id = await client.upsert(cal, appt, f"{appt.title} — {who}", description)
    appt.calendar_connection_id = conn.id
    appt.calendar_synced_at = utcnow()
    await session.flush()
    return action


async def push_pending(session: AsyncSession, org: int, limit: int = 100) -> dict:
    """Citas nuevas o cambiadas desde su última publicación (cubre panel, bot, flujos y webhooks)."""
    conn = await org_calendar(session, org)
    if not conn:
        return {"skipped": True}
    rows = (await session.scalars(select(Appointment).where(
        Appointment.organization_id == org,
        or_(Appointment.calendar_synced_at.is_(None), Appointment.updated_at > Appointment.calendar_synced_at),
        or_(Appointment.status == "scheduled", Appointment.external_event_id.is_not(None)),
        Appointment.starts_at > utcnow() - timedelta(days=1)).order_by(Appointment.id).limit(limit))).all()
    run = await common.start_run(session, conn, "appointment", "push")
    for a in rows:
        run.fetched += 1
        try:
            async with session.begin_nested():
                act = await sync_appointment(session, a, conn)
            if act == "created":
                run.created += 1
            elif act in ("updated", "deleted"):
                run.updated += 1
            else:
                run.skipped += 1
        except HubError as e:
            common.note_error(run, a.id, e)
            if not e.retryable:
                conn.status, conn.last_error = "error", str(e)[:500]
                break
    common.finish_run(run)
    await session.commit()
    return {"pushed": run.created + run.updated, "failed": run.failed}


async def pull_changes(session: AsyncSession, conn: IntegrationConnection) -> dict:
    """Cancelaciones y cambios de hora hechos en el calendario → la cita."""
    client = await client_for(session, conn)
    s = dict(conn.settings or {})
    tokens = dict(s.get("sync_tokens") or {})
    cals = {calendar_for(conn, None), *((s.get("calendars") or {}).get("agents") or {}).values()}
    run = await common.start_run(session, conn, "appointment", "pull")
    for cal in cals:
        items, nxt = await client.changes(cal, tokens.get(cal))
        for ev in items:
            run.fetched += 1
            appt = await session.scalar(select(Appointment).where(
                Appointment.calendar_connection_id == conn.id, Appointment.external_event_id == ev["id"]))
            if not appt and ev.get("appointment_id") and str(ev["appointment_id"]).isdigit():
                appt = await session.get(Appointment, int(ev["appointment_id"]))
                if appt and appt.organization_id != conn.organization_id:
                    appt = None
            if not appt:
                run.skipped += 1
                continue
            if ev["cancelled"] and appt.status == "scheduled":
                appt.status = "cancelled"
                run.updated += 1
            elif ev.get("start") and appt.status == "scheduled" and abs((ev["start"] - appt.starts_at).total_seconds()) >= 60:
                appt.starts_at = ev["start"]
                run.updated += 1
            else:
                run.skipped += 1
                continue
            appt.calendar_synced_at = utcnow() + timedelta(seconds=1)  # el cambio viene del calendario: no reenviar
        if nxt:
            tokens[cal] = nxt
    s["sync_tokens"] = tokens
    conn.settings = s
    conn.last_sync_at = utcnow()
    common.finish_run(run)
    await session.commit()
    return {"updated": run.updated, "fetched": run.fetched}


async def busy_intervals(session: AsyncSession, org: int, start: datetime, end: datetime,
                         agent_id: int | None = None) -> list[tuple[datetime, datetime]]:
    """Ocupados del calendario en el rango (excluye las citas que publicamos nosotros). [] si no hay calendario."""
    conn = await org_calendar(session, org)
    if not conn or not (conn.settings or {}).get("busy_blocks_slots", True):
        return []
    try:
        client = await client_for(session, conn)
        busy = await client.busy([calendar_for(conn, agent_id)], start, end)
    except HubError as e:
        log.warning("Calendario %s: no se pudo leer la disponibilidad: %s", conn.id, e)
        return []
    ours = (await session.execute(select(Appointment.starts_at, Appointment.duration_min).where(
        Appointment.calendar_connection_id == conn.id, Appointment.status == "scheduled",
        Appointment.external_event_id.is_not(None), Appointment.starts_at >= start - timedelta(hours=12),
        Appointment.starts_at < end))).all()
    own = {(s.astimezone(UTC), (s + timedelta(minutes=d or 30)).astimezone(UTC)) for s, d in ours}
    return [(b0, b1) for b0, b1 in busy if (b0.astimezone(UTC), b1.astimezone(UTC)) not in own]
