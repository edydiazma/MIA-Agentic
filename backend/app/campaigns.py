"""Envío masivo de plantillas (campañas). Los destinatarios llevan estado + marcas de tiempo."""

import asyncio
import logging

from sqlalchemy import select

from app import templates
from app.db import SessionLocal, set_actor
from app.identity import require_phone_for_template, wa_address
from app.models import Campaign, CampaignRecipient, Channel, Message, utcnow
from app.service import create_alert, get_or_create_conversation, record_message, wa_client

log = logging.getLogger(__name__)
SEND_INTERVAL = 0.05  # ~20 msg/s, por debajo del throughput estándar de Meta
FAILURE_ALERT_RATIO = 0.3


async def send_template_message(session, conv, tpl: dict, values: list[str], sender_type: str,
                                agent_id: int | None = None, campaign_id: int | None = None) -> Message:
    """Envía una plantilla a la conversación y la registra en el historial."""
    components, rendered = templates.build(tpl, values)
    if conv.channel.provider != "whatsapp_cloud":
        raise ValueError("Las plantillas son solo de WhatsApp")
    msg = Message(direction="out", sender_type=sender_type, sender_agent_id=agent_id, type="template",
                  text=rendered, template_name=tpl["name"], campaign_id=campaign_id)
    try:
        if not wa_address(conv.contact):
            raise ValueError("El contacto no tiene número ni usuario de WhatsApp")
        require_phone_for_template(conv.contact, tpl.get("category"))
        msg.wa_message_id = await (await wa_client(session, conv.channel)).send_template(
            wa_address(conv.contact), tpl["name"], tpl["language"], components)
        msg.status = "sent"
    except Exception as e:
        msg.status, msg.error = "failed", str(e)[:2000]
    return await record_message(session, conv, msg)


async def run_campaign(campaign_id: int) -> None:
    async with SessionLocal() as session:
        campaign = await session.get(Campaign, campaign_id)
        channel = await session.get(Channel, campaign.channel_id)
        org = campaign.organization_id
        campaign.status, campaign.started_at = "running", utcnow()
        await session.commit()

        error = None if channel.provider == "whatsapp_cloud" else "Las campañas de plantillas son solo de WhatsApp"
        try:
            catalog = [] if error else await templates.list_templates(session, channel)
        except Exception as e:
            catalog, error = [], str(e)
        tpl = next((t for t in catalog if t["name"] == campaign.template_name
                    and t["language"] == campaign.template_language), None)
        if not tpl or not tpl["supported"] or tpl["status"] != "APPROVED":
            campaign.status, campaign.finished_at = "failed", utcnow()
            await create_alert(
                session, org, severity="critical", layer="campaign", source="system",
                title=f"La campaña «{campaign.name}» no se pudo enviar",
                description=error or (tpl and tpl["unsupported_reason"]) or "Plantilla no encontrada o no aprobada",
                ref=str(campaign.id))
            return

        recipients = (await session.scalars(select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign.id, CampaignRecipient.status == "pending")
            .order_by(CampaignRecipient.id))).unique().all()

        failed = 0
        for r in recipients:
            contact = r.contact
            if contact.blocked:
                r.status, r.error = "skipped", "Contacto bloqueado"
            elif not wa_address(contact):
                r.status, r.error = "skipped", "El contacto no tiene número ni usuario de WhatsApp"
            elif tpl["category"] == "AUTHENTICATION" and not contact.wa_id:
                r.status, r.error = "skipped", "Las plantillas de autenticación necesitan el teléfono del cliente"
            elif tpl["category"] == "MARKETING" and contact.marketing_opt_out:
                r.status, r.error = "skipped", "El contacto no acepta marketing (opt-out)"
            else:
                await set_actor(session, "system")
                conv = await get_or_create_conversation(session, channel, contact, reopen=False)
                msg = await send_template_message(session, conv, tpl,
                                                  templates.personalize(campaign.params or [], contact.name),
                                                  sender_type="campaign", campaign_id=campaign.id)
                r.wa_message_id, r.error = msg.wa_message_id, msg.error
                if msg.status == "failed":
                    r.status, r.failed_at = "failed", utcnow()
                    failed += 1
                else:
                    r.status, r.sent_at = "sent", utcnow()
            await session.commit()
            await asyncio.sleep(SEND_INTERVAL)

        campaign.status, campaign.finished_at = "done", utcnow()
        await session.commit()
        sent_or_failed = sum(1 for r in recipients if r.status in ("sent", "failed"))
        if sent_or_failed and failed / sent_or_failed > FAILURE_ALERT_RATIO:
            await create_alert(session, org, severity="warning", layer="campaign", source="system",
                               title=f"Campaña «{campaign.name}»: {failed} de {sent_or_failed} envíos fallaron",
                               ref=str(campaign.id))
