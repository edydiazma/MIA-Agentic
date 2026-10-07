"""Cliente mínimo de la WhatsApp Cloud API (Graph API de Meta)."""

import hashlib
import hmac

import httpx

from app.config import get_settings

settings = get_settings()

MAX_TEXT = 4096


class WhatsAppError(Exception):
    pass


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Valida X-Hub-Signature-256. Sin WA_APP_SECRET se acepta todo (dev)."""
    if not settings.wa_app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.wa_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class WhatsAppClient:
    def __init__(self, phone_number_id: str, access_token: str | None = None):
        self.phone_number_id = phone_number_id
        self.token = access_token or settings.wa_access_token
        self.base = f"https://graph.facebook.com/{settings.wa_api_version}"

    @property
    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise WhatsAppError("Falta el token de WhatsApp de este número: conéctalo en Configuraciones → "
                                "Plataforma («Conectar WhatsApp») o define WA_ACCESS_TOKEN en el servidor.")
        return {"Authorization": f"Bearer {self.token}"}

    async def _post_message(self, payload: dict) -> str:
        payload = {"messaging_product": "whatsapp", **payload}
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(f"{self.base}/{self.phone_number_id}/messages", json=payload, headers=self._headers)
        if r.status_code >= 400:
            raise WhatsAppError(r.text)
        return r.json()["messages"][0]["id"]

    async def send_text(self, to: str, body: str) -> list[str]:
        return [
            await self._post_message({"to": to, "type": "text", "text": {"body": chunk, "preview_url": True}})
            for chunk in split_text(body)
        ]

    async def send_media(
        self, to: str, kind: str, media_id: str, caption: str | None = None, filename: str | None = None
    ) -> str:
        obj: dict = {"id": media_id}
        if caption and kind in ("image", "video", "document"):
            obj["caption"] = caption
        if filename and kind == "document":
            obj["filename"] = filename
        return await self._post_message({"to": to, "type": kind, kind: obj})

    async def upload_media(self, data: bytes, mime: str, filename: str) -> str:
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(
                f"{self.base}/{self.phone_number_id}/media",
                headers=self._headers,
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (filename, data, mime)},
            )
        if r.status_code >= 400:
            raise WhatsAppError(r.text)
        return r.json()["id"]

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        async with httpx.AsyncClient(timeout=60) as http:
            meta = await http.get(f"{self.base}/{media_id}", headers=self._headers)
            if meta.status_code >= 400:
                raise WhatsAppError(meta.text)
            info = meta.json()
            r = await http.get(info["url"], headers=self._headers)
            if r.status_code >= 400:
                raise WhatsAppError(r.text)
        return r.content, info.get("mime_type", "application/octet-stream")

    async def send_template(self, to: str, name: str, language: str, components: list[dict]) -> str:
        template: dict = {"name": name, "language": {"code": language}}
        if components:
            template["components"] = components
        return await self._post_message({"to": to, "type": "template", "template": template})

    async def send_buttons(self, to: str, body: str, buttons: list[dict]) -> str:
        """Hasta 3 botones de respuesta rápida: [{"id": "...", "title": "..."}] (título máx. 20 caracteres)."""
        return await self._post_message({"to": to, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": body[:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"][:256], "title": b["title"][:20]}}
                                   for b in buttons[:3]]}}})

    async def send_list(self, to: str, body: str, button: str, sections: list[dict]) -> str:
        """Lista interactiva: sections=[{"title": "...", "rows": [{"id", "title", "description"?}]}] (máx. 10 filas)."""
        return await self._post_message({"to": to, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": body[:4096]},
            "action": {"button": button[:20], "sections": sections}}})

    async def send_product(self, to: str, catalog_id: str, retailer_id: str, body: str) -> str:
        """Mensaje de producto del catálogo de Commerce Manager (el cliente ve foto, precio y botón)."""
        return await self._post_message({"to": to, "type": "interactive", "interactive": {
            "type": "product", "body": {"text": body[:1024]},
            "action": {"catalog_id": catalog_id, "product_retailer_id": retailer_id}}})

    async def send_image_link(self, to: str, link: str, caption: str | None = None) -> str:
        image = {"link": link, **({"caption": caption[:1024]} if caption else {})}
        return await self._post_message({"to": to, "type": "image", "image": image})

    async def list_catalog_products(self, catalog_id: str) -> list[dict]:
        url = f"{self.base}/{catalog_id}/products"
        params = {"fields": "retailer_id,name,description,price,sale_price,currency,availability,url,image_url,"
                            "brand,category", "limit": 250}
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=60) as http:
            while url:
                r = await http.get(url, params=params, headers=self._headers)
                if r.status_code >= 400:
                    raise WhatsAppError(r.text)
                body = r.json()
                out += body.get("data", [])
                url, params = body.get("paging", {}).get("next"), None
        return out

    async def list_templates(self, waba_id: str) -> list[dict]:
        url = f"{self.base}/{waba_id}/message_templates"
        params = {"fields": "name,language,status,category,components,parameter_format", "limit": 200}
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as http:
            while url:
                r = await http.get(url, params=params, headers=self._headers)
                if r.status_code >= 400:
                    raise WhatsAppError(r.text)
                body = r.json()
                out += body.get("data", [])
                url, params = body.get("paging", {}).get("next"), None
        return out

    async def mark_read(self, wa_message_id: str) -> None:
        async with httpx.AsyncClient(timeout=15) as http:
            await http.post(
                f"{self.base}/{self.phone_number_id}/messages",
                headers=self._headers,
                json={"messaging_product": "whatsapp", "status": "read", "message_id": wa_message_id},
            )
