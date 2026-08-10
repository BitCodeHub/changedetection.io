#!/usr/bin/env python3
"""
vps_scrapling_runner.py — the egress-side half of the Scrapling stealth fetcher.

This is deployed once to each VPS egress host (see scrapling_stealth.py, which
copies it to ~/.cdio_stealth/runner.py and version-guards it by the RUNNER_VERSION
marker below). changedetection.io never imports this file; it invokes it over SSH:

    ssh <egress-host> 'python3 ~/.cdio_stealth/runner.py' < request.json  > response.json

The point is the *egress IP*. When a target WAF rate-limit-bans the app host's IP
(bursts of 429, an IP-level cooldown that even a local Camoufox can't shake), the
fetch is re-run from a VPS with a clean IP. Scrapling's StealthyFetcher (Camoufox)
handles the fingerprint side; the VPS handles the IP side.

Protocol — one JSON object in on stdin, one JSON object out on stdout:

  request  {url, tier, timeout, headers, method, body, solve_cf, ignore_status}
  response {ok, status, reason, headers, html, final_url, tier_used, error}

Keep this file dependency-light and self-contained: it runs under whatever Python
the VPS has, and its only imports are stdlib + scrapling.
"""
import sys, json

RUNNER_VERSION = 3  # bump to force redeploy from scrapling_stealth.py

# Cloudflare / bot-wall fingerprints. A static fetch that trips one of these is
# re-run through the browser engine with challenge solving on.
CF_MARKERS = (
    "just a moment", "checking your browser", "attention required",
    "cf-chl", "__cf_chl", "enable javascript and cookies",
    "cf-browser-verification", "cf_chl_opt",
)


def _looks_blocked(status, html):
    if status in (403, 429, 503):
        return True
    low = (html or "")[:4000].lower()
    return any(m in low for m in CF_MARKERS)


def _static(url, timeout, headers, method, body):
    """Fast tier: no browser. curl_cffi with browser-like TLS via Scrapling."""
    from scrapling.fetchers import Fetcher
    kw = dict(stealthy_headers=True, timeout=timeout)
    if headers:
        kw["headers"] = headers
    if method == "POST":
        return Fetcher.post(url, data=body, **kw)
    return Fetcher.get(url, **kw)


def _stealth(url, timeout, solve_cf):
    """Anti-bot tier: real Camoufox browser, optional Cloudflare solving."""
    from scrapling.fetchers import StealthyFetcher
    eff = max(timeout, 120) if solve_cf else timeout
    kw = dict(headless=True, network_idle=True, timeout=eff * 1000)
    if solve_cf:
        kw["solve_cloudflare"] = True
    return StealthyFetcher.fetch(url, **kw)


def _pack(resp, tier_used):
    return {
        "ok": True,
        "status": int(getattr(resp, "status", 0) or 0),
        "reason": getattr(resp, "reason", "") or "",
        "headers": {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()},
        "html": getattr(resp, "html_content", "") or "",
        "final_url": str(getattr(resp, "url", "") or ""),
        "tier_used": tier_used,
        "error": None,
    }


def handle(req):
    url = req["url"]
    tier = req.get("tier", "auto")
    timeout = int(req.get("timeout", 60))
    headers = req.get("headers") or None
    method = (req.get("method") or "GET").upper()
    body = req.get("body")
    solve_cf = bool(req.get("solve_cf", False))

    # tier=static: fast only. tier=stealth: browser only. tier=auto: static, then
    # escalate to the browser (and then to challenge-solving) if it looks blocked.
    if tier == "stealth":
        return _pack(_stealth(url, timeout, solve_cf), "stealth")

    resp = _static(url, timeout, headers, method, body)
    if tier == "static":
        return _pack(resp, "static")

    status = int(getattr(resp, "status", 0) or 0)
    html = getattr(resp, "html_content", "") or ""
    if not _looks_blocked(status, html):
        return _pack(resp, "static")

    # Escalate: browser first, then browser + Cloudflare solve.
    resp = _stealth(url, timeout, solve_cf=False)
    status = int(getattr(resp, "status", 0) or 0)
    html = getattr(resp, "html_content", "") or ""
    if not _looks_blocked(status, html):
        return _pack(resp, "stealth")

    return _pack(_stealth(url, timeout, solve_cf=True), "stealth+cf")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(RUNNER_VERSION)
        return
    try:
        req = json.loads(sys.stdin.read() or "{}")
        out = handle(req)
    except Exception as e:
        out = {"ok": False, "status": 0, "reason": "", "headers": {},
               "html": "", "final_url": "", "tier_used": None,
               "error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
