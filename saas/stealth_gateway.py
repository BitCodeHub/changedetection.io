"""
Rivalore stealth gateway.

The one place in the SaaS that holds the VPS-fleet SSH keys. Tenant containers do
NOT hold keys and do NOT ssh anywhere — their stealth fetcher POSTs here (over the
internal network) and this service does the fleet fetch on their behalf. So a
compromised tenant container cannot reach or pivot into the egress fleet.

Endpoint:
  POST /fetch   {url, tier, timeout, headers, method, body, solve_cf, proxy}
                -> the runner's verdict JSON (ok, verdict, antibot, html, ...)
  GET  /healthz

Auth: if CDIO_STEALTH_GATEWAY_TOKEN is set, every /fetch must carry a matching
X-Gateway-Token header. The gateway is only bound to the internal SaaS network, but
the token is defence-in-depth so a tenant can't spoof arbitrary fetches at scale.

Run:  python -m saas.stealth_gateway     (or gunicorn saas.stealth_gateway:app)
Env:  CDIO_STEALTH_VPS_HOSTS, CDIO_STEALTH_GATEWAY_TOKEN, CDIO_STEALTH_TIER,
      GATEWAY_PORT (default 8077)
"""
import hmac
import os

from flask import Flask, request, jsonify

# Reuse the exact fleet logic + SSRF guard the fetcher uses — one implementation.
from changedetectionio.content_fetchers.scrapling_stealth import fleet_fetch, _ssrf_guard

app = Flask(__name__)
TOKEN = os.getenv("CDIO_STEALTH_GATEWAY_TOKEN", "")


def _authorized(req):
    if not TOKEN:
        return True
    return hmac.compare_digest(req.headers.get("X-Gateway-Token", ""), TOKEN)


@app.route("/healthz")
def healthz():
    from changedetectionio.content_fetchers.scrapling_stealth import _vps_hosts
    return {"ok": True, "hosts": [h for h, _ in _vps_hosts()], "auth": bool(TOKEN)}


@app.route("/fetch", methods=["POST"])
def fetch():
    if not _authorized(request):
        return jsonify({"ok": False, "verdict": "error", "error": "unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    url = payload.get("url")
    if not url:
        return jsonify({"ok": False, "verdict": "error", "error": "url required"}), 400

    # SSRF guard here too: the gateway must never be tricked into fetching an
    # internal/metadata address on behalf of a tenant.
    blocked = _ssrf_guard(url)
    if blocked:
        return jsonify({"ok": False, "verdict": "refused", "error": blocked}), 400

    timeout = int(payload.get("timeout") or 60)
    try:
        result = fleet_fetch(payload, timeout)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "verdict": "error", "antibot": None,
                        "status": 0, "html": "", "error": str(e)[:300]}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("GATEWAY_PORT", "8077")))
