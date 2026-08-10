# Rivalore — competitive-intelligence monitoring

Rivalore watches your competitors' pricing, product, and messaging pages and tells
you when anything changes — reaching pages that block ordinary monitors, and (on
the Analyst tier) putting an AI agent between the change and your inbox so you get a
plain-English brief instead of a raw diff.

It's a multi-tenant SaaS on top of a hardened change-monitoring engine: each account
gets its own isolated instance, and plans map to real per-instance limits.

## The two products

| | **Monitor** (non-AI) | **Analyst** (AI) |
|---|---|---|
| Price | $39/mo | $149/mo |
| Competitor pages | 200 | 1,000 |
| Check interval | 15 min | 5 min |
| Stealth anti-bot reach | ✅ | ✅ |
| AI analyst briefings | — | 🤖 interprets every change |

Plus a **Free** tier (5 pages, 3-hour checks) as the on-ramp. Prices/limits live in
`plans.py` — one edit changes the catalog everywhere.

## What separates Rivalore from a raw change monitor

1. **AI analyst, not a diff.** Analyst reads each change and reports what it *means*
   ("they dropped price 15% and killed the free tier — moving upmarket").
2. **Stealth reach.** The pages worth watching (rivals' pricing) are the ones behind
   bot-walls. Rivalore fetches them via Scrapling/Camoufox over rotating VPS egress
   IPs (see `../STEALTH_FETCHER.md`), so a blocked IP is swapped for a fresh one.
3. **Purpose-built for competitive intel**, not a generic "watch any URL" utility.

## Architecture

- **Control plane** (`saas/`) — the front door: signup, login, billing, and
  provisioning. Never serves the monitor itself.
- **One instance per tenant** — `provisioner.py` runs an isolated monitor container
  per account (Traefik routes `<slug>.<domain>` to it), stamped with the plan's env
  (`MAX_WATCHES`, interval, stealth vars, `RIVALORE_AI_AGENT`). Full data isolation,
  no shared datastore.
- **Billing** (`billing.py`) — Stripe Checkout + webhooks; entitlement is granted
  **only** by the verified webhook. Runs in a keyless stub mode locally.

## Run it locally (no Docker, no Stripe keys)

```bash
python3 -m venv saas/.venv && saas/.venv/bin/pip install flask
saas/.venv/bin/python -m saas.test_saas     # full-flow smoke test
SAAS_INSECURE_COOKIES=1 saas/.venv/bin/python -m saas.app   # http://localhost:8099
```

Signup → a free instance is (mock-)provisioned → upgrade to Monitor/Analyst via the
stub checkout → the tenant re-provisions with the new plan's env.

## Go live

1. `SAAS_PROVISIONER=docker`, set `SAAS_DOMAIN` (e.g. `app.rivalore.ai`), point
   `*.app.rivalore.ai` at the host.
2. Create the two Stripe products; set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
   `STRIPE_PRICE_MONITOR`, `STRIPE_PRICE_ANALYST`. The app refuses to start with a
   Stripe key but no webhook secret (unverifiable webhooks = forgeable entitlement).
3. Set `CDIO_STEALTH_VPS_HOSTS` to your egress fleet so paid tenants inherit it.
4. `docker compose -f saas/docker-compose.saas.yml up -d`.

## Still to build

- **The AI analyst agent itself.** `RIVALORE_AI_AGENT=1` is plumbed to the Analyst
  tenants, but the module that turns a raw change into a business-language brief and
  delivers it is the next feature. The engine already carries an LLM integration and
  the notification pipeline to build it on.
