# WA Agent Platform

An AtomChat-style platform for WhatsApp: **multimodal AI agents** with failover across LLMs, an **inbox** for
advisors, CRM, campaigns, **Scratch-style flows** (Junior / Advanced), memory, the best salesperson, a catalog, and
reports, on **Supabase** (Postgres + Realtime + Storage + Vault + cron).

| Document | Contents |
|---|---|
| [`docs/data-model.md`](docs/data-model.md) | Data model (**mandatory rule: every feature starts here**) |
| [`docs/flows.md`](docs/flows.md) | Flow JSON format, block catalog, execution |
| [`deploy/README.md`](deploy/README.md) | Deploying to AWS EC2 (Docker Compose + Caddy HTTPS, API replicas + worker, CI/CD) |
| [`docs/api.md`](docs/api.md) | Public API `/v1`, scopes, webhooks and connectors |

## Modules

| Menu | Contents |
|---|---|
| **Inicio** | Centro de Control: Meta alerts, campaigns, integrations, webhooks, AI health, my day |
| **Conversaciones / Tablero** | Live inbox, act as agent, groups, transfers, tipificaciones, tags, AI analysis and suggestions, client fields, templates, resources |
| **Clientes / Bloqueados** | Stages, normalized tags, typed custom fields, change history, CSV import |
| **Campañas** | Bulk templates by tag or list, with opt-out respected and per-recipient tracking |
| **Automatizaciones → Flujos** | Editor in **Junior** (Scratch Jr) and **Advanced** (Scratch 3 / Blockly) modes, simulator, versions, AI editing, n8n-style executions |
| **Automatizaciones → Cortex** | AI agents (several), **connections and failover**, business **memory**, **best salesperson**, product **catalog**, JSON history |
| **Automatizaciones → General** | Automated tasks and outbound webhooks (secret in Vault) |
| **Reportes** | Real time, general, stages, Inbound/Bots/Click to WA Meta, Outbound, tipificaciones and AI accuracy, SLA, agents, billing, **AI and Cortex** |
| **Negocios** | Deals board by stage (drag & drop), synced with HubSpot / Salesforce |
| **Llamadas** | WhatsApp calls: AI voice agent with transfer, answering from the browser, transcripts, recordings, summaries |
| **Atribución** | Web script/GTM (`/t/<key>.js`) with a code in wa.me links, Click to WA Meta and Google, conversions to Google Ads and Meta CAPI |
| **SaaS** | Signup with a trial, Team / Profesional / Enterprise plans with limits, Stripe, company switching, back-office at `/plataforma` |
| **Mensajes disparadores** | Trigger texts with short tracked links (`/t/l/{slug}`) or wa.me for Click to WhatsApp ads; multi-touch attribution; campaign/ad names from Meta and Google Ads; attribution pushed to HubSpot / Salesforce |
| **Omnicanal** | Instagram DM, Facebook Messenger and a web chat widget (`/w/{key}.js`) in the same inbox, flows and AI agents |
| **Calidad (QA) y coaching** | AI review of closed conversations against rubrics, human review and disputes, coaching per advisor ("Mi coaching"), automated tests for AI agents |
| **API pública** | `/v1` with API keys and scopes, rate limits, idempotency, REST Hooks for Zapier / Make / n8n (`docs/api.md`); installable advisor app (PWA) with push notifications |
| **Onboarding** | Setup wizard: Embedded Signup, number validation with fixes, profile, industry templates, AI, team, test, go-live |
| **Clientes 360 / Datos maestros** | WhatsApp username/BSUID, first/last interaction BI, products per interaction, golden record (identification keys from chats and documents), vehicles, consents, duplicates |
| **Anuncios** | Post → ad resolution, first/last source per customer, Meta/Google spend, CPL/CPA/ROAS |
| **Contact center** | Custom agent statuses + Login report, business hours per group, routing rules, client owner, SLA timers, inbound webhooks |
| **Supervisión** | Group-scoped supervisors, Monitoreo, service KPIs (AHT, ASA, attention/abandonment), transcripts |
| **Seguridad** | Roles & permissions, SSO (SAML/OIDC), 2FA, password policy, self-service reset, access audit |
| **Copiloto de IA** | Reply suggestions, drafts/rewrite, next best action, handoff and conversation summaries, supervisor assistant over reports |
| **Correo y botón de WhatsApp** | Email channel (Postmark/SendGrid/Mailgun/IMAP inbound, SMTP replies in thread), embeddable WhatsApp floating button, advisor-initiated WhatsApp calls |
| **Operación a escala** | Postgres job queue, separate voice service, panel on Supabase Realtime, reports replica, integration diagnostics (`python -m app.preflight`), load tests (`loadtest/`), runbooks (`docs/ops/`) |
| **Journeys y segmentos** | Dynamic segments over customer 360 / golden keys / vehicles / consents, multi-step journeys with waits, branches, A/B tests, frequency caps, quiet hours and goals |
| **Base de conocimiento** | RAG with pgvector: files, website, catalog, resolved conversations; hybrid search with citations; knowledge gaps |
| **Hub de integraciones** | Shopify / WooCommerce / VTEX orders, Google / Outlook calendars, Zoho / Odoo, custom REST connector builder, data export to BigQuery / S3 / GCS |
| **Seguimiento / Configuraciones** | Follow-ups, appointments; platform, messaging, conversations, AI classification, client fields, users, company, resources, appointments |

## Architecture

```
WhatsApp ─▶ /webhooks (raw in inbound_events) ─▶ ingest ─▶ flows ▸ automations ▸ AI agent
                                                        │                 │
                                                        │                 └─▶ Cortex (failover: Claude / OpenAI / compatible)
Panel (Next.js) ─▶ /api (FastAPI) ─▶ Supabase Postgres  │                       └─▶ ai_calls (latency, tokens, cost)
            ◀── /ws (real time)        ├ triggers: counters, events, close/reopen, first response
                                       ├ reporting.* (daily rollups, pg_cron)
                                       ├ Realtime Broadcast (private per-org channels)
                                       └ Vault (keys) · Storage (media, resources)
```

## Local development

Requirements: Python 3.13+, Node 22+, Postgres 17 (Homebrew `postgresql@17`) for tests.

```bash
# Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env     # DATABASE_URL → your Supabase project (Session pooler) or a local Postgres with the migrations
.venv/bin/python -m pytest          # builds a temporary Postgres from supabase/migrations and runs the 229 tests
.venv/bin/python -m app.demo        # (optional) demo data — dev environments only
.venv/bin/uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend && npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

### Database (Supabase)

```bash
npx supabase db push                          # applies supabase/migrations to the linked project
npx supabase db advisors --linked --type all  # security and performance review
supabase/tests/apply_local.sh <dir> 55432     # validates the migrations on local Postgres (plain + Supabase stubs)
```

The schema is created **only** by the migrations; the ORM (`backend/app/models.py`) is a mirror and
`tests/test_schema.py` fails if they drift apart.

## Conventions

- **Data model first:** document in `docs/data-model.md` + a migration + then code.
- Business logic that must not be skipped (counters, events, SLA) lives in **database triggers**.
- Every AI call goes through a **Cortex** (`app/ai/router.py`): never call a provider directly.
- The flow block catalog is shared: `backend/app/flows/blocks.json` = `frontend/lib/flow-blocks.json` (test included).
- Secrets go to **Vault**; the tables keep only the `uuid`.
- **Multi-company:** every query is filtered by `organization_id`; real time and webhooks only reach the event's company
  (`tests/test_isolation.py`); the company in Meta webhooks is resolved from the number or the WABA, never from configuration.
