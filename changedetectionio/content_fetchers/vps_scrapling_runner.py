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

RUNNER_VERSION = 4  # bump to force redeploy from scrapling_stealth.py

# Cloudflare / bot-wall fingerprints. A static fetch that trips one of these is
# re-run through the browser engine with challenge solving on.
CF_MARKERS = (
    "just a moment", "checking your browser", "attention required",
    "cf-chl", "__cf_chl", "enable javascript and cookies",
    "cf-browser-verification", "cf_chl_opt",
)

# Fingerprints for the major anti-bot systems, so a block can be NAMED rather than
# reported as a generic failure. Naming the wall is what lets the product tell a
# customer "Cloudflare challenge — solvable" vs "DataDome — needs residential proxy".
ANTIBOT_SIGNS = {
    "cloudflare": ("cf-chl", "__cf_chl", "cf_chl_opt", "cf-browser-verification",
                   "just a moment", "checking your browser", "attention required"),
    "datadome": ("datadome", "dd_cookie", "geo.captcha-delivery.com", "interstitial.captcha"),
    "perimeterx": ("_px", "px-captcha", "perimeterx", "human challenge", "press & hold"),
    "akamai": ("_abck", "ak_bmsc", "bm_sz", "reference #18."),
    "imperva": ("incapsula", "_incap_", "visid_incap", "incident id"),
}


def _classify(status, html):
    """Return (verdict, antibot). verdict in {ok, challenge, empty, http_error}.

    This is the heart of "never false silence": a challenge page is NOT content, and
    an empty body is NOT "no change". Only a real, non-trivial body is 'ok'."""
    low = (html or "")[:6000].lower()
    detected = next((name for name, sigs in ANTIBOT_SIGNS.items()
                     if any(s in low for s in sigs)), None)
    if detected or any(m in low for m in CF_MARKERS):
        return "challenge", detected or "unknown"
    if status in (401, 403, 429, 503):
        # A hard block status with no fingerprint we recognise — still not content.
        return "challenge", detected or "unknown"
    if status >= 400:
        return "http_error", None
    if not html or len(html.strip()) < 200:
        # A near-empty body from a page that should have content usually means a JS
        # shell we failed to render or a soft block — do not treat as real content.
        return "empty", None
    return "ok", None


def _static(url, timeout, headers, method, body, proxy=None):
    """Fast tier: no browser. curl_cffi with browser-like TLS via Scrapling."""
    from scrapling.fetchers import Fetcher
    kw = dict(stealthy_headers=True, timeout=timeout)
    if headers:
        kw["headers"] = headers
    if proxy:
        kw["proxy"] = proxy
    if method == "POST":
        return Fetcher.post(url, data=body, **kw)
    return Fetcher.get(url, **kw)


def _stealth(url, timeout, solve_cf, proxy=None):
    """Anti-bot tier: real Camoufox browser, optional Cloudflare solving. A residential
    `proxy` is what makes DataDome/PerimeterX-class targets (which flag datacenter IPs)
    actually reachable."""
    from scrapling.fetchers import StealthyFetcher
    eff = max(timeout, 120) if solve_cf else timeout
    kw = dict(headless=True, network_idle=True, timeout=eff * 1000)
    if solve_cf:
        kw["solve_cloudflare"] = True
    if proxy:
        kw["proxy"] = proxy
    return StealthyFetcher.fetch(url, **kw)


def _pack(resp, tier_used):
    status = int(getattr(resp, "status", 0) or 0)
    html = getattr(resp, "html_content", "") or ""
    verdict, antibot = _classify(status, html)
    return {
        "ok": verdict == "ok",
        "verdict": verdict,          # ok | challenge | empty | http_error
        "antibot": antibot,          # cloudflare | datadome | perimeterx | akamai | imperva | unknown | None
        "status": status,
        "reason": getattr(resp, "reason", "") or "",
        "headers": {str(k).lower(): str(v) for k, v in dict(getattr(resp, "headers", {}) or {}).items()},
        "html": html,
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
    proxy = req.get("proxy") or None            # residential proxy for hard targets

    # tier=static: fast only. tier=stealth: browser only. tier=auto: static, then
    # escalate browser -> browser+CF-solve -> (if a residential proxy is supplied)
    # browser+CF-solve over the proxy, which is the only thing that beats a
    # datacenter-IP block from DataDome/PerimeterX.
    if tier == "stealth":
        return _pack(_stealth(url, timeout, solve_cf, proxy), "stealth")

    result = _pack(_static(url, timeout, headers, method, body, proxy), "static")
    if tier == "static" or result["ok"]:
        return result

    # Escalate to the browser.
    result = _pack(_stealth(url, timeout, solve_cf=False, proxy=proxy), "stealth")
    if result["ok"]:
        return result

    # Browser + Cloudflare solve.
    result = _pack(_stealth(url, timeout, solve_cf=True, proxy=proxy), "stealth+cf")
    if result["ok"] or not proxy:
        return result

    # Last resort for datacenter-IP-flagging walls: browser + solve, over the
    # residential proxy. (Only reached when a proxy is configured and all else failed.)
    return _pack(_stealth(url, timeout, solve_cf=True, proxy=proxy), "stealth+cf+residential")


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
