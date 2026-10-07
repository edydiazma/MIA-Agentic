# WA Agent Platform

An AtomChat-style platform for WhatsApp: a **multimodal AI agent** answering customers, plus a **web panel**
with an inbox for advisors, CRM, campaigns, automations, follow-ups, appointments and reports.

## Panel modules

| Menu | What it does |
|---|---|
| **Inicio (Centro de Control)** | Account health: Meta alerts (marketing opt-outs, paused/rejected templates, number quality), campaigns from the last 7 days, integrations, webhooks and "my day" |
| **Conversaciones** | Live inbox: filters, "act as agent", groups, transfers, closing with a typification (tipificación), quick replies (`/shortcut`), templates outside the 24 h window, resource library, client side panel |
| **Tablero** | Live: queue waiting for an advisor, load per advisor, online/available status |
| **Clientes / Clientes Bloqueados** | Contacts with stage (lead → prospect → client), tags, notes, CSV import, blocking (a blocked contact's messages are ignored) |
| **Campañas** | Bulk template sends by tag or phone list, personalized with `{{nombre}}`, with tracking for sent / delivered / read / failed / not delivered. Respects marketing opt-outs |
| **Automatizaciones** | Automated tasks (welcome, keyword replies and transfers, business hours, close after inactivity), outbound webhooks signed with HMAC, **Cortex** (AI agent + knowledge base) |
| **Reportes** | Real time, general report, contact stages, Inbound (summary, bots, Click to WA Meta), Outbound (summary, template campaigns, individual templates, webhooks), typifications, service level (SLA), agent status, billing (pricing reported by Meta) |
| **Seguimiento** | Follow-ups per client with due dates, and the appointments calendar |
| **Configuraciones** | Platform (WhatsApp numbers), messaging, conversations (typifications, SLA, auto-assignment), AI, users and groups, company, resource manager, appointments |

**Later phases** (they appear in the menu marked as such): flow builder, calls and voice agents,
Click to WA Google / web traffic (needs GTM), real syncing with HubSpot / Salesforce / Google Ads / Meta Ads,
multi-company setup and plans.

## The AI agent

- Understands **text, images, PDFs, voice notes (transcribed), locations, contacts and buttons**.
- Configurable provider: **Claude** (default `claude-opus-5-5`, cached prompt, server-side fallback on refusals) or **OpenAI**.
- Answers from the **knowledge base** (PDF/txt/md uploaded in Cortex) and knows if the customer came from an ad.
- Tools: `transfer_to_human` (with a destination group), `check_availability` and `book_appointment` (when appointments are enabled).
- When it transfers: assigns the online, available advisor with the least load in the group; outside business hours it tells the customer.
- If the model fails or refuses, it transfers to a human instead of going silent.

## Running locally

```bash
# Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in WA_*, ANTHROPIC_API_KEY / OPENAI_API_KEY and JWT_SECRET
.venv/bin/python -m app.demo    # (optional) demo data to explore the panel
.venv/bin/uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Log in at http://localhost:3000 with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. The demo creates advisors
`luis@demo.com`, `carolina@demo.com` and `jorge@demo.com` (password `demo12345`). **Do not run the demo against a production database.**

Tests: `cd backend && .venv/bin/python -m pytest` (end-to-end flows with WhatsApp and the AI simulated).

## Connecting WhatsApp

1. In Meta for Developers, open your app with the WhatsApp product and copy the **Phone number ID**, the **WhatsApp Business Account ID** and a **permanent System User token** (`whatsapp_business_messaging`, `whatsapp_business_management`).
2. Expose the backend over HTTPS (locally: `ngrok http 8000` or `cloudflared tunnel`).
3. Under WhatsApp > Configuration > Webhook: URL `https://YOUR-DOMAIN/webhooks/whatsapp`, verify token = `WA_VERIFY_TOKEN`. Subscribe to the fields:
   `messages`, `message_template_status_update`, `message_template_quality_update`, `phone_number_quality_update`, `account_update`, `account_alerts`.
4. Set `WA_APP_SECRET` so the `X-Hub-Signature-256` signature is validated.

More numbers can be added from **Configuraciones → Plataforma**.

## Outbound webhooks

Each event is sent as a `POST` with JSON `{event, data, sent_at}` and the header
`X-Signature-256: sha256=HMAC_SHA256(secret, body)`. Events: `message.new`, `message.status`,
`conversation.updated`, `conversation.handoff`, `conversation.closed`, `appointment.created`, `contact.updated`.
After 10 consecutive failures the webhook is deactivated and an alert is raised.

## Architecture

```
WhatsApp ──webhook──▶ FastAPI ─┬─▶ ingest: messages, statuses/billing, opt-outs, Meta alerts, Click to WA
                               ├─▶ automations ─▶ AI agent (Claude/OpenAI + knowledge base + tools)
                               ├─▶ campaigns (bulk template sending)
                               ├─▶ REST /api ◀── Next.js (panel)
                               ├─▶ WebSocket /ws (real time + advisor presence) ◀── Next.js
                               └─▶ outbound webhooks (HMAC)
```

## Pending for production

- Alembic migrations (today the tables are created with `create_all`: a model change on an existing database needs a migration)
- Media on S3/GCS, Redis pub/sub for the WebSocket with several replicas, a job queue for campaigns
- Media templates (image/document headers) and templates with variables in the header
- Multi-company setup (tenants), plans and limits if the platform will be resold
