# Public API (`/v1`)

REST API to integrate a CRM, ERP, landing pages or automation tools (Zapier, Make, n8n) with the platform.
Field names are in English; messages and descriptions in Spanish. Interactive reference: **`/v1/docs`**
(OpenAPI: `/v1/openapi.json`). Data model: `docs/data-model.md` §12.4.

Available on the **Profesional** and **Enterprise** plans (feature `api`).

## Authentication

Create a key in **Configuraciones → API** (admins only). The full key is shown **once**:

```
wak_live_ab12cd34_9fK2...32 characters
```

Send it on every request:

```
Authorization: Bearer wak_live_ab12cd34_...
```

(`X-API-Key: wak_live_...` also works.) Only the SHA-256 of the key is stored; the visible prefix
(`wak_live_ab12cd34`) identifies it in the panel. Keys can be rotated (new secret, same scopes; the old one stops
working immediately), revoked (its connector webhooks are deactivated) or given an expiry date.

| Status | `error.code` | When |
|---|---|---|
| 401 | `unauthorized` | missing, malformed, unknown or revoked key |
| 401 | `key_expired` | key past `expires_at` |
| 402 | `payment_required` | the company's plan doesn't include the API, or the trial ended |
| 403 | `forbidden` | company suspended |
| 403 | `missing_scope` | the key lacks the scope the endpoint needs |

## Scopes

| Scope | Allows |
|---|---|
| `contacts:read` | list, search and read contacts |
| `contacts:write` | create/update contacts, tags, custom fields |
| `conversations:read` | list and read conversations |
| `conversations:write` | assign, close and typify conversations |
| `messages:read` | read messages (also included in `GET /v1/conversations/{id}`) |
| `messages:send` | send text or WhatsApp templates |
| `deals:read` / `deals:write` | read / create, move, win, lose deals |
| `webhooks:manage` | subscribe and unsubscribe webhooks (REST Hooks) |
| `reports:read` | period summary |

## Errors

Always the same envelope:

```json
{"error": {"code": "validation_error", "message": "phone: Field required"}}
```

Codes: `bad_request` (400), `unauthorized`, `key_expired` (401), `payment_required` (402), `forbidden`,
`missing_scope` (403), `not_found` (404), `conflict`, `idempotency_conflict`, `idempotency_in_progress`,
`outside_window`, `opted_out`, `contact_blocked` (409), `validation_error` (422), `rate_limited` (429).

## Rate limits

Each key has its own limit per minute (default 120, editable per key), shared by every backend replica.
Every response carries:

```
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 117
X-RateLimit-Reset: 42        # seconds until the window resets
```

Over the limit: `429 rate_limited` with `Retry-After` (seconds). Every authenticated call is logged
(`api_requests`, ~1 month) and summarized per key and day in the panel.

## Idempotency

`POST` endpoints accept `Idempotency-Key` (≤ 200 characters, kept 24 h):

- same key + same body → the original response is replayed (header `Idempotent-Replayed: true`);
- same key + different body → `409 idempotency_conflict`;
- while the first request is still running → `409 idempotency_in_progress`;
- if the first request failed, the key is released and you can retry with it.

Use it for anything a retry must not duplicate: sending messages, creating deals, subscribing webhooks.

## Pagination

Lists return `{"data": [...], "next_cursor": "..."}` newest first. Pass `?limit=` (1–100, default 50) and
`?cursor=<next_cursor>` for the next page; `next_cursor: null` means it was the last one. Most lists also accept
`updated_since=<ISO-8601>` for incremental syncs.

## Endpoints

| Method | Path | Scope | Notes |
|---|---|---|---|
| GET | `/v1/me` | any | company + key (connection test for Zapier/Make/n8n) |
| GET | `/v1/contacts` | contacts:read | `q`, `tag`, `stage`, `updated_since` |
| GET | `/v1/contacts/{id}` | contacts:read | |
| POST | `/v1/contacts` | contacts:write | **upsert** by `phone`, then `email`: 201 created / 200 updated; tags are added |
| PATCH | `/v1/contacts/{id}` | contacts:write | `tags` replaces all tags; `custom_fields` by field key |
| POST | `/v1/contacts/{id}/tags` | contacts:write | `{"add": [...], "remove": [...]}` |
| GET | `/v1/conversations` | conversations:read | `status` (`bot`, `human`, `closed`, `open`), `contact_id`, `assigned_agent_id`, `group_id`, `channel_id` |
| GET | `/v1/conversations/{id}` | conversations:read | last `messages` (≤ 100) if the key has `messages:read` |
| GET | `/v1/conversations/{id}/messages` | messages:read | paginated |
| POST | `/v1/conversations/{id}/messages` | messages:send | `text`, or `template` (WhatsApp) outside the window |
| POST | `/v1/messages` | messages:send | to a phone: creates contact and conversation on the first WhatsApp number (or `channel_id`) |
| POST | `/v1/conversations/{id}/assign` | conversations:write | `agent_id` (null = queue), `group_id` |
| POST | `/v1/conversations/{id}/close` | conversations:write | `typification` (name) |
| GET | `/v1/deals` | deals:read | `status`, `stage`, `contact_id`, `updated_since` |
| GET | `/v1/deals/{id}` | deals:read | |
| POST | `/v1/deals` | deals:write | `contact_id` or `phone`; `stage` of the pipeline (default the first) |
| PATCH | `/v1/deals/{id}` | deals:write | moving to `won`/`lost` closes it (and records the conversion) |
| POST | `/v1/deals/{id}/won` · `/lost` | deals:write | `{"amount"}` · `{"reason"}` |
| GET | `/v1/reports/summary` | reports:read | `start`, `end` (default last 7 days, company time zone) |
| GET | `/v1/events` | any | events available for webhooks |
| GET | `/v1/webhooks` | webhooks:manage | subscriptions created through the API |
| POST | `/v1/webhooks` | webhooks:manage | REST Hooks subscribe (below) |
| DELETE | `/v1/webhooks/{id}` | webhooks:manage | unsubscribe (panel webhooks can't be deleted here) |
| GET | `/v1/webhooks/sample/{event}` | any | `[sample data]` for Zapier's field mapping |

**Messaging window.** WhatsApp only allows free text within 24 h of the customer's last message; outside it,
send an approved template (`{"template": {"name", "language", "values": [...]}}`) or you get
`409 outside_window`. Messenger/Instagram follow Meta's window; web chat has none. Marketing templates to
contacts who opted out → `409 opted_out`. Messages are sent on behalf of the business (`sender_type: agent`);
pass `agent_id` to attribute them to an advisor. Conversation events done through the API are recorded with
actor `api`; deals and tags created through it have source `api`.

## Webhooks and REST Hooks (Zapier, Make, n8n)

Events: `message.new`, `message.status`, `conversation.updated`, `conversation.handoff`,
`conversation.closed`, `appointment.created`, `contact.updated`.

Subscribe (the connector calls this when a Zap/scenario/workflow is turned on):

```
POST /v1/webhooks
X-Connector: zapier            # zapier | make | n8n | api (default)
{"url": "https://hooks.zapier.com/hooks/standard/123/abc", "events": ["message.new"]}

201 {"id": 42, "url": "...", "events": ["message.new"], "source": "zapier", "active": true,
     "secret": "9c1f...48 hex"}
```

Unsubscribe when it is turned off: `DELETE /v1/webhooks/42`.

Each delivery is a `POST` with JSON `{"event", "data", "sent_at"}` signed with the subscription's secret:

```
X-Signature-256: sha256=<hex HMAC-SHA256 of the raw body with the secret>
```

Verify it before trusting the payload (constant-time comparison). After 10 consecutive failed deliveries the
subscription is deactivated and an alert is created; revoking the key deactivates all of its subscriptions.

### Zapier app (private integration)

1. Authentication: **API Key** → field `api_key`; add header `Authorization: Bearer {{bundle.authData.api_key}}`.
   Test: `GET {{base}}/v1/me`; connection label `{{organization.name}}`.
2. Triggers (REST Hook): subscribe `POST /v1/webhooks` with header `X-Connector: zapier`, body
   `{"url": "{{bundle.targetUrl}}", "events": ["message.new"]}`; unsubscribe
   `DELETE /v1/webhooks/{{bundle.subscribeData.id}}`; perform list `GET /v1/webhooks/sample/message.new`.
3. Actions: `POST /v1/contacts` (create/update contact), `POST /v1/messages` (send WhatsApp),
   `POST /v1/deals`, `PATCH /v1/deals/{id}`. Searches: `GET /v1/contacts?q=`.

### Make / n8n

Use an HTTP module/node with the same header. For instant triggers, create a "custom webhook" in Make or a
Webhook node in n8n and register its URL with `POST /v1/webhooks` (`X-Connector: make` / `n8n`); verify
`X-Signature-256` with the returned secret.

## Examples

```bash
API=https://panel.yourcompany.com
KEY=wak_live_...

# Create or update a contact
curl -s $API/v1/contacts -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: crm-contact-881" \
  -d '{"phone": "573001234567", "name": "Ana Pérez", "email": "ana@example.com",
       "tags": ["crm"], "custom_fields": {"ciudad": "Bogotá"}}'

# Send a WhatsApp template to a phone (works outside the 24 h window)
curl -s $API/v1/messages -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-5521-confirmation" \
  -d '{"phone": "573001234567", "template": {"name": "recordatorio", "language": "es", "values": ["lunes 10 am"]}}'

# Open conversations assigned to an advisor
curl -s "$API/v1/conversations?status=open&assigned_agent_id=7&limit=20" -H "Authorization: Bearer $KEY"

# Win a deal
curl -s -X POST $API/v1/deals/310/won -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"amount": 149000000}'
```

Verify a webhook signature (Python):

```python
import hashlib, hmac

def valid(secret: str, raw_body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
```

## Advisor app (PWA) and push notifications

The panel is installable (manifest + service worker; it opens on Conversaciones). In **Configuraciones →
Notificaciones** each advisor enables Web Push on each device and chooses what to be notified about: a
conversation assigned to them, a new customer message on their conversations while they're not connected, an
incoming WhatsApp call, and (optional) new unassigned conversations in their groups. Requires the server
variables `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT` (generate a pair with
`npx web-push generate-vapid-keys`).
