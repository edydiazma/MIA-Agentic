"""Carga datos de DEMOSTRACIÓN para recorrer el panel sin conectar WhatsApp.

Uso (solo en una base local o de pruebas, nunca en producción):
    .venv/bin/python -m app.demo

No envía nada a WhatsApp ni llama a la IA. Los contactos usan números ficticios 57300000xxxx.
Los eventos (handoff, asignación, primera respuesta, cierre) los generan los triggers de la base al
recorrer los estados en orden, igual que en la operación real.
"""

import asyncio
import random
from datetime import timedelta

from sqlalchemy import select, text

from app.auth import hash_password
from app.db import SessionLocal, set_actor
from app.main import bootstrap
from app.models import (
    Agent,
    AgentGroup,
    Alert,
    Appointment,
    Automation,
    Campaign,
    CampaignRecipient,
    Channel,
    Contact,
    ContactTag,
    Conversation,
    ConversationTag,
    FollowUp,
    Group,
    Message,
    QuickReply,
    Tag,
    Typification,
    utcnow,
)
from app.settings_store import org_id

NAMES = ["Ana Gómez", "Carlos Pérez", "Laura Ruiz", "Andrés Mejía", "Sofía Castro", "Juan Rojas", "Valentina Díaz",
         "Felipe Torres", "Camila Vargas", "Diego Ramírez", "Paula Herrera", "Mateo López", "Isabella Moreno",
         "Santiago Cruz", "Mariana Ortiz", "Daniel Silva", "Gabriela Ríos", "Nicolás Peña"]
QUESTIONS = ["Hola, ¿qué precio tiene el Onix?", "¿Tienen financiación?", "Quiero agendar un test drive",
             "¿Cuál es el horario del taller?", "Necesito una cotización", "¿Reciben mi carro como parte de pago?"]
REASONS = ["El cliente pidió hablar con un asesor", "Solicitud de financiación", "Reclamo por garantía",
           "Negociación de precio"]
TYPIFICATIONS = ["Venta", "Consulta resuelta", "Cotización enviada", "Sin respuesta"]


async def main() -> None:
    await bootstrap()
    org = org_id()
    rnd = random.Random(7)
    now = utcnow()
    async with SessionLocal() as s:
        if await s.scalar(select(Contact.id).where(Contact.organization_id == org, Contact.wa_id.like("57300000%"))):
            print("Los datos de demostración ya existen.")
            return
        channel = await s.scalar(select(Channel).where(Channel.organization_id == org).limit(1))
        if not channel:
            channel = Channel(organization_id=org, name="WhatsApp demo", phone_number_id="DEMO",
                              display_phone="+57 300 000 0000")
            s.add(channel)
            await s.flush()
        typ = {t.name: t for t in (await s.scalars(
            select(Typification).where(Typification.organization_id == org))).all()}

        ventas = Group(organization_id=org, name="Ventas",
                       description="Compra de vehículos nuevos y usados, cotizaciones y financiación")
        posventa = Group(organization_id=org, name="Posventa", description="Taller, garantías y reclamos")
        s.add_all([ventas, posventa])
        await s.flush()
        agents = []
        for name, email, group in [("Luis Martínez", "luis@demo.com", ventas),
                                   ("Carolina Ruiz", "carolina@demo.com", ventas),
                                   ("Jorge Salas", "jorge@demo.com", posventa)]:
            a = Agent(organization_id=org, email=email, name=name, password_hash=hash_password("demo12345"))
            s.add(a)
            await s.flush()
            s.add(AgentGroup(agent_id=a.id, group_id=group.id))
            agents.append(a)

        tags = {}
        for name in ("vip", "chevrolet", "usados", "taller", "financiacion"):
            tags[name] = Tag(organization_id=org, name=name)
            s.add(tags[name])
        await s.flush()

        contacts = []
        for i, name in enumerate(NAMES):
            c = Contact(organization_id=org, wa_id=f"57300000{i:04d}", name=name,
                        stage=rnd.choice(["lead", "lead", "prospect", "client", "lost"]),
                        created_at=now - timedelta(days=rnd.randint(0, 20)))
            s.add(c)
            await s.flush()
            for t in rnd.sample(list(tags), 2):
                s.add(ContactTag(contact_id=c.id, tag_id=tags[t].id))
            contacts.append(c)
        await s.commit()

        for i, c in enumerate(contacts):
            start = now - timedelta(days=rnd.randint(0, 6), hours=rnd.randint(0, 10), minutes=rnd.randint(0, 59))
            final = "closed" if i == 17 else rnd.choice(["bot", "human", "human", "closed", "closed"])
            from_ad = i % 4 == 0
            await set_actor(s, "contact")
            conv = Conversation(organization_id=org, contact_id=c.id, channel_id=channel.id, status="bot",
                                ai_agent_id=channel.default_ai_agent_id, created_at=start, last_message_at=start,
                                ad_source_type="ad" if from_ad else None,
                                ad_source_id="120210000001" if from_ad else None,
                                ad_headline="Chevrolet Onix 0 km — bono de $5M" if from_ad else None)
            s.add(conv)
            await s.flush()

            def msg(direction, sender, body, at, agent_id=None, conv_id=conv.id):
                return Message(organization_id=org, conversation_id=conv_id, direction=direction, sender_type=sender,
                               type="text", text=body, status="read" if direction == "out" else "received",
                               created_at=at, sender_agent_id=agent_id,
                               pricing_category="service" if direction == "out" else None,
                               billable=True if sender == "bot" else None)

            t = start
            for direction, sender, body in [("in", "contact", rnd.choice(QUESTIONS)),
                                            ("out", "bot", "¡Hola! Con gusto te ayudo. ¿Qué modelo te interesa?"),
                                            ("in", "contact", "El Onix, ¿qué versiones tienen?")]:
                t += timedelta(minutes=rnd.randint(1, 4))
                s.add(msg(direction, sender, body, t))
            await s.flush()

            if final == "human" or (final == "closed" and i % 2):
                t += timedelta(minutes=1)
                s.add(msg("out", "bot", "Te comunico con un asesor, en un momento te atiende.", t))
                await set_actor(s, "bot")
                conv.status, conv.handoff_reason, conv.handoff_at = "human", rnd.choice(REASONS), t
                conv.group_id = rnd.choice([ventas.id, ventas.id, posventa.id])
                await s.flush()
                if not (final == "human" and i % 3 == 0):  # algunas quedan en cola sin asignar
                    agent = agents[2] if conv.group_id == posventa.id else rnd.choice(agents[:2])
                    await set_actor(s, "system")
                    conv.assigned_agent_id = agent.id
                    await s.flush()
                    t += timedelta(minutes=rnd.choice([1, 2, 3, 4, 8]))
                    s.add(msg("out", "agent", f"Hola {c.name.split()[0]}, soy {agent.name.split()[0]}. "
                                              "Te envío la ficha técnica.", t, agent.id))
                    await s.flush()
            if final == "closed":
                await set_actor(s, "agent")
                conv.status = "closed"
                conv.typification_id = typ["Spam" if i == 17 else rnd.choice(TYPIFICATIONS)].id
                await s.flush()
            if i % 3 == 1:
                s.add(ConversationTag(conversation_id=conv.id, tag_id=tags["chevrolet"].id, source="ai",
                                      confidence=0.9))
            if i % 5 == 0:
                s.add(FollowUp(organization_id=org, contact_id=c.id, conversation_id=conv.id, agent_id=agents[0].id,
                               due_at=now + timedelta(hours=rnd.randint(-20, 48)),
                               note="Llamar para confirmar interés"))
            if i % 6 == 0:
                day = now + timedelta(days=rnd.randint(1, 5))
                s.add(Appointment(organization_id=org, contact_id=c.id, conversation_id=conv.id, title="Test drive",
                                  starts_at=day.replace(hour=15, minute=0, second=0, microsecond=0),
                                  created_by_type="bot", notes="Onix Premier"))
            await s.commit()

        for name, total in [("BONOS 05 OCTUBRE", 6), ("TERCER DÍA CHEVROLET", 10)]:
            camp = Campaign(organization_id=org, name=name, channel_id=channel.id, template_name="promo_bonos",
                            template_language="es", params=["{{nombre}}"], audience={"tag": "chevrolet"},
                            status="done", created_at=now - timedelta(hours=8), started_at=now - timedelta(hours=8),
                            finished_at=now - timedelta(hours=8))
            s.add(camp)
            await s.flush()
            for c in contacts[:total]:
                st = rnd.choice(["delivered", "read", "read", "sent", "failed"])
                at = camp.started_at
                s.add(CampaignRecipient(
                    campaign_id=camp.id, contact_id=c.id, status=st, sent_at=at,
                    delivered_at=at if st in ("delivered", "read") else None,
                    read_at=at if st == "read" else None, failed_at=at if st == "failed" else None,
                    error_code=131049 if st == "failed" else None,
                    error="131049: Mensaje no entregado para mantener la calidad del ecosistema"
                    if st == "failed" else None))

        for c, ago in ((contacts[3], timedelta(hours=4)), (contacts[8], timedelta(days=1))):
            c.marketing_opt_out, c.opt_out_at = True, now - ago
            s.add(Alert(organization_id=org, severity="warning", layer="contact", source="meta", ref=c.wa_id,
                        title="Un contacto pidió dejar de recibir marketing", created_at=c.opt_out_at,
                        description="Solo puedes enviarle mensajes de utilidad y autenticación; "
                                    "los mensajes de marketing generarán un error."))
        blocked = contacts[17]
        blocked.blocked, blocked.blocked_reason, blocked.blocked_at = True, "Spam", now - timedelta(days=2)

        s.add_all([
            Automation(organization_id=org, name="Bienvenida", type="welcome",
                       config={"message": "¡Hola! Bienvenido. Soy el asistente virtual, ¿en qué te ayudo?"}),
            Automation(organization_id=org, name="Horario de atención", type="business_hours",
                       config={"days": [0, 1, 2, 3, 4, 5], "start": "08:00", "end": "18:00",
                               "message": "Nuestros asesores atienden de lunes a sábado de 8:00 a 18:00. "
                                          "Te responderemos apenas abramos."}),
            # Desactivada para que los datos de demostración no se cierren solos.
            Automation(organization_id=org, name="Cierre por inactividad", type="inactivity_close",
                       config={"hours": 24}, enabled=False),
            QuickReply(organization_id=org, shortcut="saludo", text="¡Hola! Soy tu asesor, ¿en qué te puedo ayudar?"),
            QuickReply(organization_id=org, shortcut="ficha", text="Te comparto la ficha técnica del vehículo 👇"),
        ])
        await s.commit()
        await s.execute(text("select reporting.refresh_range(:o, (now() - interval '21 days')::date, now()::date)"),
                        {"o": org})
        await s.commit()
    print("Datos de demostración cargados. Asesores: luis@demo.com / carolina@demo.com / jorge@demo.com "
          "(clave demo12345)")


if __name__ == "__main__":
    asyncio.run(main())
