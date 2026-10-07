"""Atribución de WhatsApp como campos mapeables al CRM (solo push: nunca se leen de vuelta).

Fuente: la atribución más reciente del contacto (o la de la conversación del negocio), su primer toque y el
mensaje disparador (wa_links). Ver docs/data-model.md §10.5.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attribution, AttributionTouch, WaLink

PREFIX = "attribution."
CHANNEL_LABELS = {"meta_ctwa": "Click to WhatsApp (Meta)", "google_ads": "Google Ads", "meta_ads_web": "Meta Ads (web)",
                  "paid_other": "Otros pagos", "organic_web": "Web orgánica", "offline": "Offline (QR, SMS)",
                  "campaign": "Campañas WhatsApp", "direct": "Directo"}
# campo local -> etiqueta (orden = orden en el panel de mapeo)
FIELDS = {
    "attribution.channel": "Canal",
    "attribution.channel_label": "Canal (nombre)",
    "attribution.utm_source": "UTM source",
    "attribution.utm_medium": "UTM medium",
    "attribution.utm_campaign": "UTM campaign",
    "attribution.utm_content": "UTM content",
    "attribution.utm_term": "UTM term",
    "attribution.campaign_name": "Campaña",
    "attribution.ad_name": "Anuncio",
    "attribution.ad_group_name": "Conjunto / grupo de anuncios",
    "attribution.keyword": "Palabra clave",
    "attribution.gclid": "GCLID",
    "attribution.ctwa_clid": "CTWA click id",
    "attribution.link_name": "Mensaje disparador",
    "attribution.first_touch_channel": "Primer toque: canal",
    "attribution.first_touch_campaign": "Primer toque: campaña",
    "attribution.first_touch_at": "Primer toque: fecha",
    "attribution.last_touch_at": "Último toque: fecha",
    "attribution.touches": "Toques",
    # Fuente del cliente por anuncio (docs/data-model.md §16)
    "attribution.first_source_label": "Fuente (primer toque)",
    "attribution.first_source_campaign": "Campaña de origen",
    "attribution.first_source_ad_id": "Anuncio de origen (ID)",
    "attribution.last_source_label": "Última fuente",
    "attribution.ad_headline": "Título del anuncio",
}


# Cliente 360 (docs/data-model.md §14): también solo push, mismo mecanismo que attribution.*
CUSTOMER_PREFIX = "customer."
CUSTOMER_FIELDS = {
    "customer.wa_username": "Usuario de WhatsApp",
    "customer.wa_bsuid": "ID de WhatsApp (BSUID)",
    "customer.first_interaction_at": "Primera interacción",
    "customer.last_interaction_at": "Última interacción",
    "customer.conversations_count": "Conversaciones",
    "customer.products": "Productos de interés",
    "customer.last_typification": "Última tipificación",
}


def is_attribution(field: str) -> bool:
    """Campos calculados que solo se envían al CRM (atribución y cliente 360)."""
    return field.startswith(PREFIX) or field.startswith(CUSTOMER_PREFIX)


async def load_customer(session: AsyncSession, contact_id: int) -> dict:
    from app.models import Contact, InteractionProduct, Typification

    c = await session.get(Contact, contact_id)
    if c is None:
        return {}
    names = (await session.scalars(
        select(InteractionProduct.name).where(InteractionProduct.contact_id == contact_id)
        .order_by(InteractionProduct.created_at.desc()).limit(50))).all()
    products = list(dict.fromkeys(names))[:20]
    typ = await session.get(Typification, c.last_typification_id) if c.last_typification_id else None
    return {
        "customer.wa_username": c.wa_username,
        "customer.wa_bsuid": c.wa_bsuid,
        "customer.first_interaction_at": c.first_interaction_at.isoformat() if c.first_interaction_at else None,
        "customer.last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
        "customer.conversations_count": c.conversations_count,
        "customer.products": ", ".join(products) or None,
        "customer.last_typification": typ.name if typ else None,
    }


async def load(session: AsyncSession, contact_id: int, conversation_id: int | None = None) -> dict:
    """Valores de todos los campos attribution.* y customer.*."""
    customer = await load_customer(session, contact_id)
    return {**customer, **await _load_attribution(session, contact_id, conversation_id)}


async def _load_attribution(session: AsyncSession, contact_id: int, conversation_id: int | None = None) -> dict:
    """Valores de todos los campos attribution.* (vacío si el contacto no tiene atribución)."""
    q = select(Attribution).where(Attribution.contact_id == contact_id)
    attr = None
    if conversation_id:
        attr = (await session.scalars(q.where(Attribution.conversation_id == conversation_id))).first()
    if attr is None:
        attr = (await session.scalars(q.order_by(Attribution.created_at.desc(), Attribution.id.desc()).limit(1))).first()
    if attr is None:
        return {}
    from app.models import Contact

    contact = await session.get(Contact, contact_id)
    first = None
    if attr.first_touch_id:
        first = (await session.scalars(select(AttributionTouch).where(
            AttributionTouch.id == attr.first_touch_id, AttributionTouch.conversation_id == attr.conversation_id)
            .limit(1))).first()
    link = await session.get(WaLink, attr.link_id) if attr.link_id else None
    first_campaign = None
    if first is not None:
        first_link = link if first.link_id == attr.link_id else (
            await session.get(WaLink, first.link_id) if first.link_id else None)
        first_campaign = first.utm_campaign or (first_link.name if first_link else None) or first.ad_id
    return {
        "attribution.channel": attr.channel,
        "attribution.channel_label": CHANNEL_LABELS.get(attr.channel, attr.channel),
        "attribution.utm_source": attr.utm_source,
        "attribution.utm_medium": attr.utm_medium,
        "attribution.utm_campaign": attr.utm_campaign,
        "attribution.utm_content": attr.utm_content,
        "attribution.utm_term": attr.utm_term,
        "attribution.campaign_name": attr.platform_campaign_name or attr.utm_campaign,
        "attribution.ad_name": attr.ad_name,
        "attribution.ad_group_name": attr.ad_group_name,
        "attribution.keyword": attr.keyword or attr.utm_term,
        "attribution.gclid": attr.gclid,
        "attribution.ctwa_clid": attr.ctwa_clid,
        "attribution.link_name": link.name if link else None,
        "attribution.first_touch_channel": first.channel if first else attr.channel,
        "attribution.first_touch_campaign": first_campaign,
        "attribution.first_touch_at": (first.occurred_at if first else attr.created_at).isoformat(),
        "attribution.last_touch_at": (attr.last_touch_at or attr.created_at).isoformat(),
        "attribution.touches": attr.touches or 1,
        "attribution.first_source_label": contact.first_source_label if contact else None,
        "attribution.first_source_campaign": contact.first_source_campaign if contact else None,
        "attribution.first_source_ad_id": contact.first_source_ad_id if contact else None,
        "attribution.last_source_label": contact.last_source_label if contact else None,
        "attribution.ad_headline": attr.ad_headline,
    }


# Propiedades que crea el botón de HubSpot: (nombre, etiqueta, campo local, tipo)
HUBSPOT_GROUP = "whatsapp_attribution"
HUBSPOT_PROPERTIES = [
    ("wa_channel", "WA canal", "attribution.channel_label", "string"),
    ("wa_utm_source", "WA UTM source", "attribution.utm_source", "string"),
    ("wa_utm_medium", "WA UTM medium", "attribution.utm_medium", "string"),
    ("wa_utm_campaign", "WA UTM campaign", "attribution.utm_campaign", "string"),
    ("wa_utm_content", "WA UTM content", "attribution.utm_content", "string"),
    ("wa_utm_term", "WA UTM term", "attribution.utm_term", "string"),
    ("wa_campaign_name", "WA campaña", "attribution.campaign_name", "string"),
    ("wa_ad_name", "WA anuncio", "attribution.ad_name", "string"),
    ("wa_keyword", "WA palabra clave", "attribution.keyword", "string"),
    ("wa_gclid", "WA GCLID", "attribution.gclid", "string"),
    ("wa_ctwa_clid", "WA CTWA click id", "attribution.ctwa_clid", "string"),
    ("wa_trigger_link", "WA mensaje disparador", "attribution.link_name", "string"),
    ("wa_first_touch_channel", "WA primer toque: canal", "attribution.first_touch_channel", "string"),
    ("wa_first_touch_campaign", "WA primer toque: campaña", "attribution.first_touch_campaign", "string"),
    ("wa_touches", "WA toques", "attribution.touches", "number"),
]

# Salesforce: campo estándar + personalizados que se mapean solo si existen en el objeto
SALESFORCE_STANDARD = [("LeadSource", "attribution.channel_label")]
SALESFORCE_CUSTOM = [
    ("WA_Channel__c", "attribution.channel_label"),
    ("WA_UTM_Source__c", "attribution.utm_source"),
    ("WA_UTM_Medium__c", "attribution.utm_medium"),
    ("WA_UTM_Campaign__c", "attribution.utm_campaign"),
    ("WA_Campaign_Name__c", "attribution.campaign_name"),
    ("WA_Ad_Name__c", "attribution.ad_name"),
    ("WA_Keyword__c", "attribution.keyword"),
    ("WA_GCLID__c", "attribution.gclid"),
    ("WA_Trigger_Link__c", "attribution.link_name"),
]
