# Deploying to AWS EC2

Architecture: **one EC2 instance** with Docker Compose (Caddy + backend + frontend). Database, Storage, Realtime,
Vault and cron live in **Supabase**. Caddy obtains and renews the HTTPS certificate on its own (Meta requires HTTPS for
the webhook).

```
Internet ──443──▶ Caddy ─┬─ /api, /ws, /webhooks, /health ─▶ backend (FastAPI, 1 process)
                          └─ everything else ───────────────▶ frontend (Next.js standalone)
backend ──▶ Supabase (Session pooler, Storage, Vault)   ·   ──▶ WhatsApp Cloud API   ·   ──▶ Claude / OpenAI
```

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
- Calls **answered by the AI voice agent**: the backend creates the WebRTC session (aiortc) and connects it to the realtime model. That needs **UDP** between Meta and the instance:
  - Security Group: allow inbound UDP on the ephemeral range (e.g. `32768-60999`) or, better, put the voice agent behind a TURN server (coturn) and set `VOICE_STUN_URLS`.
  - Docker: use `network_mode: host` for the backend service (Docker NAT breaks ICE), or run the voice agent outside Docker.
  - Unverified on EC2 so far (it was tested locally over loopback): validate a real call before enabling it for customers.

## 5. Updates

```bash
cd wa-agent-platform/deploy && ./update.sh
```

## 6. Operations

| Topic | How |
|---|---|
| Logs | `docker compose logs -f backend` (rotation: 5 × 20 MB per service) |
| Health | `/health`; Docker restarts the containers if they fail (`restart: unless-stopped`) |
| Backups | Supabase (daily backups / PITR depending on the plan). The EC2 holds no data: it can be recreated from git + `.env` |
| Secrets | `.env` on the instance (permissions 600). Provider keys and channel tokens added from the panel go to Supabase Vault |
| Scaling | This design is **1 backend process** (in-memory WebSocket and workers). To scale out: move the frontend to Supabase Realtime (the triggers already publish) and the workers to a separate process |
| Monitoring | CloudWatch Agent (CPU/memory/disk) + an external `/health` check (e.g. Route 53 health checks or UptimeRobot) |
