# Deploying to AWS EC2

Architecture: **one EC2 instance** with Docker Compose (Caddy + backend replicas + worker + frontend). Database,
Storage, Vault and cron live in **Supabase**. Caddy obtains and renews the HTTPS certificate on its own (Meta requires
HTTPS for the webhook) and load-balances across the backend replicas.

```
Internet ──443──▶ Caddy ─┬─ /api, /ws, /webhooks, /t, /health ─▶ backend × BACKEND_REPLICAS (ROLE=api)
                          └─ everything else ───────────────────▶ frontend (Next.js standalone)
                                                worker (ROLE=worker): flows, inactivity, conversions, CRM, catalog…
backend/worker ──▶ Supabase Postgres (Session pooler: queries + LISTEN/NOTIFY + advisory locks), Storage, Vault
               ──▶ WhatsApp Cloud API · Claude / OpenAI · Google Ads / Meta / HubSpot / Salesforce / Stripe
```

**How it scales** (docs/data-model.md §12.1):
- The **API** is stateless: each replica holds its own WebSockets and live events travel between replicas through
  Postgres `LISTEN/NOTIFY` (big events through `realtime_spill`). Agent presence is cluster-wide (each replica
  publishes who is connected every 20 s). Outbound webhooks and push notifications are sent once, by the replica that
  produced the event.
- The **worker** runs the background tasks. Each task holds a Postgres advisory lock, so it runs once in the cluster
  even with two workers (the second one is a hot standby: it takes over in ≤ 15 s if the first dies).
- **Session pooler is required** (`DATABASE_URL` on port 5432 of `pooler.supabase.com`, or the direct host):
  `LISTEN` and advisory locks need a session connection; the Transaction pooler (6543) does not work.
- **Connections per process:** API replica = pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) + 1 (LISTEN); worker = pool
  + 1 (all its locks share one connection). Keep the total under your Supabase plan's pooler client limit
  (e.g. 2 API + 1 worker with 5+5 → ~33).

## 1. Infrastructure (once)

1. **EC2:** Ubuntu 24.04 LTS, `t3.small` (2 GB) to start; `t3.medium` if you'll handle several agents and campaigns.
   20 GB gp3 disk. Same region as Supabase (lower latency).
2. **Elastic IP** associated with the instance.
3. **Security Group:** inbound 80/tcp and 443/tcp (+443/udp for HTTP/3) from `0.0.0.0/0`; 22/tcp **only from your IP**
   (or use SSM Session Manager and close 22).
4. **DNS:** an `A` record `panel.yourcompany.com` → Elastic IP.
5. **Docker** on the instance:
   ```bash
   sudo apt-get update && sudo apt-get install -y ca-certificates curl git
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker ubuntu && newgrp docker
   ```
6. (Recommended) a 2 GB swap so the frontend build doesn't run out of memory on `t3.small`:
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

## 2. Database (from your machine, before each deploy that brings migrations)

```bash
cd wa-agent-platform
npx supabase db push            # applies supabase/migrations/* to the linked project
npx supabase db advisors --linked --type all
```

## 3. Application

```bash
git clone <your-repo> wa-agent-platform && cd wa-agent-platform/deploy
cp .env.example .env && nano .env      # fill in DOMAIN, DATABASE_URL, keys, JWT_SECRET (openssl rand -hex 32)
docker compose up -d --build
docker compose logs -f backend          # should say the schema exists and the bootstrap ran
curl https://panel.yourcompany.com/health
```

Log in at `https://panel.yourcompany.com` with `ADMIN_EMAIL` / `ADMIN_PASSWORD`, then **change the password**.

## 4. Meta (WhatsApp)

In Meta for Developers → your app → WhatsApp → Configuration:
- Callback URL: `https://panel.yourcompany.com/webhooks/whatsapp`
- Verify token: the value of `WA_VERIFY_TOKEN`
- Fields: `messages`, `calls`, `message_template_status_update`, `message_template_quality_update`,
  `phone_number_quality_update`, `account_update`, `account_alerts`
- `WA_ACCESS_TOKEN` must be a **permanent System User** token (not the 24 h one).

## 4b. Phase 2 integrations (URLs to register with each provider)

Replace `https://panel.yourcompany.com` with your `PUBLIC_BASE_URL`.

| Provider | Where it is configured | URL |
|---|---|---|
| **Meta: Embedded Signup** (each SaaS customer connects their number) | App → Facebook Login for Business → Configuration (`META_EMBEDDED_SIGNUP_CONFIG_ID`) | Allowed domain: `panel.yourcompany.com` |
| **Meta: Calls** | Webhook field `calls` (same URL as messages) | `https://panel.yourcompany.com/webhooks/whatsapp` |
| **Google Ads** (offline conversions) | Google Cloud → OAuth client (web) → Authorized redirect URI | `https://panel.yourcompany.com/api/attribution/oauth/google_ads/callback` |
| **HubSpot** | Developer app → Auth → Redirect URL | `https://panel.yourcompany.com/api/integrations/hubspot/callback` |
| **HubSpot** (webhooks, optional) | Developer app → Webhooks | `https://panel.yourcompany.com/api/integrations/hubspot/webhook` |
| **Salesforce** | Setup → App Manager → Connected App → Callback URL | `https://panel.yourcompany.com/api/integrations/salesforce/callback` |
| **Stripe** | Developers → Webhooks (events: `checkout.session.completed`, `customer.subscription.*`, `invoice.paid`, `invoice.payment_failed`) | `https://panel.yourcompany.com/api/billing/webhook` |
| **Web tracking** | Each customer pastes the script (or the GTM tag) shown in Configuraciones → Atribución web | `https://panel.yourcompany.com/t/<public_key>.js` |

Stripe: create one *Price* per plan and save its id in the back-office (`/plataforma` → Plans).

### Voice (AI voice agents)

- Calls **answered by an advisor from the browser**: the audio goes directly browser ↔ Meta (WebRTC); the server only relays signaling. No extra ports needed.
- Calls **answered by the AI voice agent**: the API replica that receives the call webhook creates the WebRTC
  session (aiortc) **in its own memory** and connects it to the realtime model. Consequences:
  - The media session is not shared between replicas: a later `terminate` webhook can land on another replica.
    **While AI voice agents are enabled, run `BACKEND_REPLICAS=1`** (the worker can still be separate). Making
    voice sessions cluster-aware (routing call events to the replica that owns the session) is pending.
  - It needs **UDP** between Meta and the instance: Security Group inbound UDP on the ephemeral range
    (e.g. `32768-60999`) or, better, a TURN server (coturn) via `VOICE_STUN_URLS`; and Docker NAT breaks ICE, so the
    backend service needs `network_mode: host` (then remove `expose` and point Caddy to `host.docker.internal:8000`)
    or the voice agent must run outside Docker.
  - Unverified on EC2 so far (it was tested locally over loopback): validate a real call before enabling it for customers.

## 5. Updates

```bash
cd wa-agent-platform/deploy && ./update.sh
```

`update.sh` is a staged update: it builds the new image, replaces the **worker** first (tasks move to it as soon as it
takes the locks), then starts **new API replicas next to the old ones**, waits until they report healthy
(`/health/ready`), and only then stops the old ones (open panels reconnect their WebSocket to a new replica). If the
new replicas don't become healthy, they are removed and the old ones keep serving.

### CI/CD (GitHub Actions)

- `.github/workflows/ci.yml` — on every push/PR: backend lint + full test suite on Postgres 17 (the test harness
  builds the database from `supabase/migrations`), frontend type-check + production build.
- `.github/workflows/deploy.yml` — manual (*Actions → Deploy → Run workflow*, choose a ref) or on a `v*` tag. It
  refuses to deploy a commit whose CI didn't pass, connects over SSH, checks out that exact commit, runs
  `update.sh`, and verifies `https://$DOMAIN/health/ready`.
- Repository secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY` (private key, OpenSSH format), `EC2_KNOWN_HOSTS`
  (`ssh-keyscan -t ed25519 <host>`), `APP_DOMAIN`. Optional variable `EC2_APP_DIR` (default `~/wa-agent-platform`).
  Create a GitHub *environment* named `production` to require an approval before each deploy.
- Database migrations stay manual and go first: `npx supabase db push`, then deploy.

## 6. Operations

| Topic | How |
|---|---|
| Logs | `docker compose logs -f backend worker` (rotation: 5 × 20 MB per service). `LOG_FORMAT=json` → one JSON object per line with `request_id` (also returned as the `X-Request-ID` header) and `org_id`; ship them with the CloudWatch agent |
| Health | `/health` = process alive. `/health/ready` = database reachable + LISTEN connected (API) + fresh heartbeat (worker); 503 otherwise. Docker's HEALTHCHECK uses `/health/ready`; `restart: unless-stopped` restarts crashed containers |
| Workers | `worker_heartbeats` table: one row per process with role, version and which tasks it leads (`loops`) |
| Errors | Optional `SENTRY_DSN` |
| Backups | Supabase (daily backups / PITR depending on the plan). The EC2 holds no data: it can be recreated from git + `.env` |
| Secrets | `.env` on the instance (permissions 600). Provider keys and channel tokens added from the panel go to Supabase Vault |
| Scaling | `BACKEND_REPLICAS` (or `docker compose up -d --scale backend=3`). A second worker (`--scale worker=2`) is a hot standby. Beyond one instance: same images on several EC2s (or ECS) behind an ALB — nothing in the backend is tied to one host except AI voice sessions (see Voice) |
| Monitoring | CloudWatch Agent (CPU/memory/disk + JSON logs) and an external check on `https://$DOMAIN/health/ready` (Route 53 health checks or UptimeRobot) |
| Metrics | `/metrics` (Prometheus text) on each container, **not published by Caddy**: scrape `backend:8000/metrics` and `worker:8000/metrics` from inside the Docker network (Prometheus/Grafana Agent, or the CloudWatch agent's Prometheus support). Set `METRICS_TOKEN` to require `Authorization: Bearer`. Main series: `wa_http_requests_total`, `wa_http_request_duration_seconds`, `wa_ws_connections`, `wa_realtime_*`, `wa_rate_limited_total`, `wa_loop_leader`, `wa_loop_restarts_total`, `wa_heartbeat_age_seconds` |
| Rate limits | Public tracking endpoints (`/t/*`) are limited per IP, shared across replicas (429 + `Retry-After`) |
