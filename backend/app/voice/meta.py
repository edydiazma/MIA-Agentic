"""Cliente de la WhatsApp Business Calling API (Cloud API).

Referencia: developers.facebook.com/docs/whatsapp/cloud-api/calling
- POST /{phone-number-id}/calls  {messaging_product, call_id, action: pre_accept|accept|reject|terminate,
                                   session: {sdp_type: "answer", sdp}}
- POST /{phone-number-id}/settings {"calling": {"status": "ENABLED"|"DISABLED", "call_hours": {...}}}
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
        self.base = f"https://graph.facebook.com/{settings.wa_api_version}"

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
