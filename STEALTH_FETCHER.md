# Stealth fetcher — Scrapling/Camoufox via VPS egress

This fork adds a content fetcher, **`html_scrapling_stealth`**, that beats the
anti-bot defences the stock fetchers can't: it pairs Scrapling's `StealthyFetcher`
(Camoufox — a real browser fingerprint, JS execution, optional Cloudflare-Turnstile
solving) with an SSH hop to a pool of VPS egress IPs. Fingerprint bans are handled
by Camoufox; *IP* bans (bursts of 429 that follow the address, not the fingerprint)
are handled by swapping to a fresh VPS IP on the same request.

It appears in the watch's **Fetch Method** dropdown as
"Stealth (Scrapling/Camoufox via VPS egress — anti-bot)".

## How it fits the existing app

changedetection.io already has a pluggable `content_fetchers/` layer. Two files were
added and one line wired in — nothing in the core was changed:

| File | Role |
|------|------|
| `changedetectionio/content_fetchers/scrapling_stealth.py` | The fetcher class (`html_scrapling_stealth`). Runs inside the app; decides local-vs-VPS, rotates the fleet, parses the response into `self.content` / `self.status_code` / `self.headers`. |
| `changedetectionio/content_fetchers/vps_scrapling_runner.py` | The egress-side runner. Auto-deployed to each VPS at `~/.cdio_stealth/runner.py`; takes a JSON request on stdin, does the Scrapling fetch, returns JSON. |
| `changedetectionio/content_fetchers/__init__.py` | One import line so `available_fetchers()` lists it in the dropdown. |

The fetcher conforms to the same `Fetcher` base contract as `html_requests`
(`run()` populates `content`, `status_code`, `headers`, `raw_content`), so diffing,
notifications, the API, and history all work unchanged.

## Configuration (environment variables)

| Var | Default | Meaning |
|-----|---------|---------|
| `CDIO_STEALTH_MODE` | `auto` | `local` (Scrapling in-process), `vps` (always via a VPS egress), or `auto` (try local static first, escalate to a VPS on a 403/429/503 or Cloudflare fingerprint). |
| `CDIO_STEALTH_VPS_HOSTS` | `nexa-vps` | Comma-separated ssh hosts (must resolve via `~/.ssh/config`), rotated per-target. A host may carry an interpreter override inline: `srv1322419=/root/.venv/bin/python`. |
| `CDIO_STEALTH_REMOTE_PYTHON` | `python3` | Default interpreter on hosts without an inline override. |
| `CDIO_STEALTH_TIER` | `auto` | `static` (curl_cffi, no browser), `stealth` (full Camoufox), or `auto` (static → browser → browser+CF-solve on a block). |
| `CDIO_STEALTH_SSH_OPTS` | sane batch/timeout set | Extra ssh options. |
| `CDIO_STEALTH_SSH_RETRIES` | `3` | Retries for transient ssh-transport errors (fail2ban refusal, reset), with backoff. |

## Fleet (this deployment)

Egress hosts are the Hostinger VPS fleet, referenced by `~/.ssh/config` alias:

| ssh alias | egress IPv4 | Scrapling |
|-----------|-------------|-----------|
| `nexa-vps` | 187.77.7.221 | system `python3` |
| `srv1322419` | 93.127.216.72 | `/root/.venv/bin/python` (venv) |

Recommended production setting once both are provisioned:

```
CDIO_STEALTH_MODE=vps
CDIO_STEALTH_VPS_HOSTS=nexa-vps,srv1322419=/root/.venv/bin/python
CDIO_STEALTH_TIER=auto
```

## Running

Same as upstream — `docker compose up -d` or `python3 changedetection.py -d ./data`.
For `mode=vps`/`auto` the **app host only needs `ssh`** (key access to the fleet);
Scrapling lives on the VPS. For `mode=local`, uncomment `scrapling>=0.3.14` in
`requirements.txt` and run `scrapling install` on the app host.

The runner auto-deploys to each VPS on first use (one ssh connection, idempotent per
process). Nothing to install by hand beyond Scrapling being importable by the
configured interpreter on each host.

## Notes / guardrails

- **SSH rate-limiting:** opening many ssh connections in a short burst can trip a
  VPS fail2ban and get the app host's IP temporarily refused. Normal operation
  (one fetch per watch per interval) is nowhere near that rate; transient refusals
  self-heal via `CDIO_STEALTH_SSH_RETRIES`.
- **Politeness:** the stealth path is for sites you're allowed to monitor. Keep watch
  intervals reasonable — a browser fetch from a fresh IP is powerful, not a licence
  to hammer a target.
- **Egress consistency:** the same target hashes to the same egress host so cookies /
  challenge state stay warm; different targets fan out across the fleet.
