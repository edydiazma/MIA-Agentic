"""Cliente de la WhatsApp Business Calling API (Cloud API).

Referencia: developers.facebook.com/docs/whatsapp/cloud-api/calling
- POST /{phone-number-id}/calls  {messaging_product, call_id, action: pre_accept|accept|reject|terminate,
                                   session: {sdp_type: "answer", sdp}}
- POST /{phone-number-id}/settings {"calling": {"status": "ENABLED"|"DISABLED", "call_hours": {...}}}
Llamadas iniciadas por la empresa (§19.1):
- POST /{phone-number-id}/calls {messaging_product, to | recipient, action: "connect",
                                 session: {sdp_type: "offer", sdp}} → {"calls": [{"id": "wacid..."}]}
  El SDP answer llega en el webhook "connect"; los estados RINGING / ACCEPTED / REJECTED y el terminate también.
- Permiso del cliente: mensaje interactivo {type: "call_permission_request", action: {name: "call_permission_request"},
  body: {text}}; la respuesta llega como interactive.type "call_permission_reply" {response: accept|reject,
  is_permanent, expiration_timestamp, response_source}. Temporal = 7 días. Límites por cliente: 1 solicitud cada
  24 h y 2 cada 7 días.
- GET /{phone-number-id}/call_permissions?user_wa_id=… → {permission: {status: temporary|permanent|no_permission,
  expiration_time}, actions: [...]}
"""

import httpx

from app.config import get_settings

settings = get_settings()
DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


class CallingError(Exception):
    pass


class CallingClient:
    def __init__(self, phone_number_id: str, access_token: str | None = None):
        self.phone_number_id = phone_number_id
        self.token = access_token or settings.wa_access_token
        self.base = f"{settings.wa_graph_base.rstrip('/')}/{settings.wa_api_version}"  # mock en pruebas de carga

    async def _post(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(f"{self.base}/{self.phone_number_id}/{path}", json=payload,
                                headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 400:
            raise CallingError(r.text[:1000])
        return r.json() if r.content else {}

    async def action(self, call_id: str, action: str, sdp: str | None = None,
                     callback_data: str | None = None) -> dict:
        payload: dict = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
        if sdp:
            payload["session"] = {"sdp_type": "answer", "sdp": sdp}
        if callback_data:
            payload["biz_opaque_callback_data"] = callback_data
        return await self._post("calls", payload)

    async def connect(self, to: str, sdp_offer: str, callback_data: str | None = None) -> str:
        """Llamada iniciada por la empresa; devuelve el id de la llamada (wacid…)."""
        from app.identity import is_bsuid

        payload: dict = {"messaging_product": "whatsapp", "action": "connect",
                         "session": {"sdp_type": "offer", "sdp": sdp_offer}}
        payload.update({"recipient": to} if is_bsuid(to) else {"to": to})
        if callback_data:
            payload["biz_opaque_callback_data"] = callback_data
        data = await self._post("calls", payload)
        calls = data.get("calls") or []
        if not calls or not calls[0].get("id"):
            raise CallingError(f"Meta no devolvió el id de la llamada: {str(data)[:300]}")
        return calls[0]["id"]

    async def request_permission(self, to: str, body: str | None = None) -> str:
        """Mensaje interactivo de solicitud de permiso de llamada; devuelve el wamid."""
        from app.identity import is_bsuid

        interactive: dict = {"type": "call_permission_request", "action": {"name": "call_permission_request"}}
        if body:
            interactive["body"] = {"text": body[:1024]}
        payload: dict = {"messaging_product": "whatsapp", "recipient_type": "individual", "type": "interactive",
                         "interactive": interactive}
        payload.update({"recipient": to} if is_bsuid(to) else {"to": to})
        data = await self._post("messages", payload)
        return (data.get("messages") or [{}])[0].get("id", "")

    async def permission_status(self, user_wa_id: str) -> dict:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(f"{self.base}/{self.phone_number_id}/call_permissions",
                               params={"user_wa_id": user_wa_id}, headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code >= 400:
            raise CallingError(r.text[:1000])
        return r.json()

    async def set_calling(self, enabled: bool, hours: dict | None, timezone: str) -> dict:
        calling: dict = {"status": "ENABLED" if enabled else "DISABLED"}
        if enabled and hours:
            calling["call_hours"] = {
                "status": "ENABLED",
                "timezone_id": timezone,
                "weekly_operating_hours": [
                    {"day_of_week": DAYS[d], "open_time": hours.get("start", "08:00").replace(":", ""),
                     "close_time": hours.get("end", "18:00").replace(":", "")}
                    for d in hours.get("days", [0, 1, 2, 3, 4])
                ],
            }
        elif enabled:
            calling["call_hours"] = {"status": "DISABLED"}
        return await self._post("settings", {"calling": calling})
