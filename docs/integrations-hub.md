# Integrations hub (§21.3)

Backend: `backend/app/hub/*`, `backend/app/routers/hub.py`, Zoho/Odoo through `app/crm` (`/api/integrations`).
Background work: loop `app.hub.runner:hub_loop` (every 60 s) enqueues `hub.sync_connection` (queue `crm`) and
`hub.export` (queue `default`).

## Server configuration (env)

| Variable | Used by |
|---|---|
| `PUBLIC_BASE_URL`, `FRONTEND_BASE_URL` | OAuth redirects and webhook URLs |
| `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, `SHOPIFY_API_VERSION` (2026-10) | Shopify public app (optional: stores can also connect with a custom-app token) |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Google Calendar |
| `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT` (common) | Outlook / Microsoft 365 |
| `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_ACCOUNTS_URL` (https://accounts.zoho.com, or .eu/.in) | Zoho CRM |

Odoo, WooCommerce, VTEX, custom connectors and export destinations use per-organization credentials stored in Vault.

## Redirect / webhook URLs to register

- Shopify app redirect: `{PUBLIC_BASE_URL}/api/hub/oauth/shopify/callback`
- Google / Microsoft redirect: `{PUBLIC_BASE_URL}/api/hub/oauth/google_calendar/callback`,
  `{PUBLIC_BASE_URL}/api/hub/oauth/microsoft_calendar/callback`
- Zoho redirect: `{PUBLIC_BASE_URL}/api/integrations/zoho/callback`
- Store / connector webhooks (shown on each connection): `{PUBLIC_BASE_URL}/webhooks/hub/{provider}/{connection_id}`
  - Shopify: topics `orders/create`, `orders/updated` (HMAC `X-Shopify-Hmac-Sha256` with the app secret).
  - WooCommerce: "Order created / updated" with a secret (HMAC `X-WC-Webhook-Signature`).
  - VTEX: `POST /api/orders/hook/config` with this URL and header `X-Hub-Secret: <webhook secret>`.
  - Custom: per definition (`webhooks.secret_header`, HMAC-SHA256 hex/base64 or shared token).

## Exports and Looker Studio

1. Create a BigQuery dataset and a service account with *BigQuery Data Editor* + *BigQuery Job User* on it.
2. In **Configuraciones → Exportación de datos** add a BigQuery export (project, dataset, location, service-account
   JSON). Each dataset lands in a table `wa_<dataset>` (JSONL load jobs, schema auto-detected, `WRITE_APPEND`).
3. Incremental runs append changed rows. For "current state" reporting create a view per table, e.g.
   `select * except(rn) from (select *, row_number() over (partition by id order by updated_at desc) rn
   from dataset.wa_contacts) where rn = 1`.
4. In Looker Studio: *Create → Data source → BigQuery* → project → dataset → the view. Join `wa_orders` with
   `wa_attributions` on `attribution_id` for revenue by campaign.

Sensitive columns (phone, name, email, message text, click ids, order customer) are excluded unless the export
enables *include sensitive data* and the user also has `exports.contacts` (+ `exports.conversations` for messages).
Parquet requires `pyarrow` on the server; without it parquet exports are written as CSV.
