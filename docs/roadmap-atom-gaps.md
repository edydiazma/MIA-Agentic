# Backlog: gaps vs. AtomChat (Atom Service Desk) manual

Source: every page of the Atom user manual, integrations manual, "Bot portable" and release notes (read 2026-10-12).
Only features we lack (MISSING) or only partly have (PARTIAL). Size: S / M / L.
Priority after the onboarding wizard (docs/data-model.md §13).

## Recommended order

1. **Custom agent statuses + status-time log** (name, icon, receives conversations) — basis for real-time dashboard,
   "Login" report and correct routing. M
2. **Business hours per group** (time zone, several ranges per day, out-of-hours message, "assign anyway" and
   "pause bot" toggles, general schedule copied to groups). M
3. **SLA timer automations**: no agent reply in X min → message / reassign within group; no client reply in bot for
   X min → handoff / typify. M
4. **Typification-triggered follow-up sequences** (typify "pending reply" → 30m/1h/2h/3h messages → auto-typify);
   new flow trigger "typified". S/M
5. **Client owner + sticky routing + auto-reassign on user deactivation**. M
6. **Start a conversation / bulk template from the client list** (checkbox selection, sending number, template,
   assign / bot on-off, continue vs. new conversation). S/M
7. **Richer typifications** (keyword, section positive/negative/follow-up, per group, bot reactivation after N h,
   required sale fields: amount, currency, invoice). M
8. **Historical KPIs**: AHT (assignment → close), abandonment, not attended, attention rate, unique clients vs. cases,
   bot-only vs. agent cases; "Links" report. M
9. **Supervisor scoped to their groups**, then **custom roles with granular permissions**. M → L
10. **Quick replies** with attachments, category, per-group availability, `{{agent_name}}` / `{{client_name}}` /
    `{{group_name}}`. S
11. **Security**: SSO (SAML/OIDC, e.g. OneLogin), 2FA, password policy (expiry, complexity), IP-restricted login,
    self-service password reset. M

## Other gaps

| Module | Gap | Size |
|---|---|---|
| Panel | In-app notification bell + sound toggles (Web Push exists) | S/M |
| Conversations | Sub-states NEW / RETURNING / REASSIGNED with inbox vs. active tabs and counters | M |
| Conversations | Filters by date, typification, conversation ID; search by agent / group | S |
| Conversations | Agent-initiated WhatsApp call button (calling is inbound only) | M/L |
| Conversations | Event timeline UI (data exists in `conversation_events`) | S |
| Leads | Outbound call work queue; product on lead; bulk CSV of leads into a group | M |
| Leads | Callback reminder notification when a follow-up is due | S |
| Clients | Filters by channel, agent, created/updated date | S |
| Clients | CSV import assigning group / agent | S |
| Real time | Counters since 00:00, time in current status, per-group agent stats | S |
| Reports | "Login" report (depends on status log) | M |
| Monitoring | Supervisor audit view (stuck-in-bot, bulk manual assignment); single conversation transcript download | M / S |
| Users | License counter on the users page; employee code field | S |
| Groups | Allowed transfer targets between groups; channels served per group | S |
| Automations | "Schedule call" action | S |
| Settings | Currency custom-field type (verify); sticky agent; assignment fallback when nobody is online | S |
| Integrations | Email channel (forwarded inbox) | L |
| Integrations | Embeddable WhatsApp floating button (single / multi-agent) | S |
| Integrations | Web chat: open from CSS selector, auto-open delay, initial message to bot, clear on open, avatar, themes | S |
| API | `/v1/messages` with `assign` / `pause` / `groupName` / `clientOwnerId` | S |

Atom's release notes stop in 2020; our AI, flows, attribution, voice, QA and public API go beyond what Atom documents.
