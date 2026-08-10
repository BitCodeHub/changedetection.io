"""
Scrapling stealth fetcher for changedetection.io — anti-bot fetching with a
clean egress IP.

Why this exists: the stock `html_requests` fetcher loses to Cloudflare/DataDome
walls, and even a local headless browser loses once the *IP* is rate-limit-banned
(bursts of 429 that follow the address, not the fingerprint). This fetcher pairs
Scrapling's StealthyFetcher (Camoufox — real browser fingerprint, JS, optional
Cloudflare-Turnstile solving) with an SSH hop to a pool of VPS egress IPs, so a
banned address is swapped for a fresh one on the same request.

Backend name (in the fetch-backend dropdown): `html_scrapling_stealth`.

Configuration (env):
  CDIO_STEALTH_MODE       auto | local | vps        (default: auto)
      local — run Scrapling in-process on the app host.
      vps   — always route the fetch through a VPS egress host.
      auto  — try local first; on a block/ban (403/429/503 or a Cloudflare
              fingerprint) re-run from a VPS. This is the documented pattern.
  CDIO_STEALTH_VPS_HOSTS  comma-separated ssh hosts (default: nexa-vps)
      Each name must resolve via ~/.ssh/config. Rotated per-fetch so no single
      egress IP absorbs the whole crawl. A host may carry an interpreter override
      inline as `host=/path/to/python` when Scrapling lives in a venv there, e.g.
      "nexa-vps,srv1322419=/root/.venv/bin/python".
  CDIO_STEALTH_REMOTE_PYTHON  default interpreter on the VPS (default: python3)
      Used for any host without an inline `=interpreter` override.
  CDIO_STEALTH_TIER       static | stealth | auto    (default: auto)
      static  — curl_cffi with browser TLS, no browser (fast, cheap).
      stealth — full Camoufox browser.
      auto    — static, escalate to browser, then browser + CF-solve on a block.
  CDIO_STEALTH_STRICT     1 | 0                       (default: 1)
      1 — a detected anti-bot challenge is NOT returned as content; the fetch
          raises PageUnloadable so the watch shows an explicit "access lost" error
          instead of silently diffing a challenge page ("never false silence").
      0 — return whatever came back (legacy behaviour).
  CDIO_STEALTH_RESIDENTIAL_PROXY  proxy URL (default: unset)
      A residential/mobile proxy (http://user:pass@host:port) for the hardest walls
      (DataDome/PerimeterX/Akamai flag datacenter IPs). A per-watch proxy set in
      changedetection.io overrides this. When set, the auto tier adds a final
      browser+CF-solve-over-proxy attempt.
  CDIO_STEALTH_SSH_OPTS   extra ssh options (default: sane batch/timeout set)

Only `local` mode needs Scrapling installed on the app host; `vps`/`auto` need it
on the VPS (it is) plus SSH key access.

Prove-before-promise: call fetcher().validate(url) (or run this module as a CLI
against one or more URLs) to get a structured {watchable, verdict, antibot, ...}
verdict without raising — used by the add-watch flow to tell a customer up front
whether a page is watchable and, if not, exactly which wall is in the way.
"""
import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import time

from flask_babel import lazy_gettext as _l
from loguru import logger

from changedetectionio.content_fetchers.base import Fetcher
from changedetectionio.content_fetchers.exceptions import (
    BrowserStepsInUnsupportedFetcher, EmptyReply, Non200ErrorCodeReceived, PageUnloadable,
)

# Path to the egress-side runner, co-located with this module. Deployed to each
# VPS at REMOTE_RUNNER_PATH and version-guarded by the runner's own marker.
#
# These are home-RELATIVE (no leading ~ or /): ssh runs remote commands from the
# remote home directory, so a relative path resolves there. Crucially this avoids
# a leading `~`, which the LOCAL shell would expand to the app host's home before
# ssh ever sees it — sending a bogus path to the VPS.
_RUNNER_LOCAL = os.path.join(os.path.dirname(__file__), "vps_scrapling_runner.py")
REMOTE_RUNNER_DIR = ".cdio_stealth"
REMOTE_RUNNER_PATH = f"{REMOTE_RUNNER_DIR}/runner.py"

_DEFAULT_SSH_OPTS = "-o ConnectTimeout=15 -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

# Hosts already confirmed to carry the current runner this process lifetime, so we
# don't re-deploy on every fetch.
_deployed_hosts = set()


def _env(name, default):
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _ssrf_guard(url):
    """Refuse URLs that resolve to a private/reserved IP or carry a parser-differential
    payload — the same SSRF check the core html_requests fetcher applies. Without it,
    routing a fetch to http://169.254.169.254/ (cloud metadata) or an internal host
    would leak that host through this fetcher (and, in local mode, from the app host
    itself). Overridable with ALLOW_IANA_RESTRICTED_ADDRESSES=true, matching the core.
    Returns an error string if the URL must be refused, else None."""
    from changedetectionio.strtobool import strtobool
    if strtobool(os.getenv("ALLOW_IANA_RESTRICTED_ADDRESSES", "false")):
        return None
    try:
        from changedetectionio.validate_url import is_url_private_or_parser_confused
        if is_url_private_or_parser_confused(url):
            return ("Refused: this URL resolves to a private/reserved address or contains a "
                    "parser-differential payload. Set ALLOW_IANA_RESTRICTED_ADDRESSES=true to allow.")
    except Exception:
        # If the validator itself is unavailable, fail closed for obviously-internal hosts.
        low = (url or "").lower()
        if any(h in low for h in ("169.254.169.254", "localhost", "127.0.0.1", "[::1]", "metadata")):
            return "Refused: internal/metadata address."
    return None


def _vps_hosts():
    """Return [(ssh_host, remote_python), ...]. An entry may carry an inline
    interpreter override as `host=/path/to/python`; otherwise the default from
    CDIO_STEALTH_REMOTE_PYTHON (or `python3`) is used."""
    default_py = _env("CDIO_STEALTH_REMOTE_PYTHON", "python3")
    hosts = []
    for raw in _env("CDIO_STEALTH_VPS_HOSTS", "nexa-vps").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if "=" in raw:
            host, py = raw.split("=", 1)
            hosts.append((host.strip(), py.strip() or default_py))
        else:
            hosts.append((raw, default_py))
    return hosts


def _ssh_opts():
    return _env("CDIO_STEALTH_SSH_OPTS", _DEFAULT_SSH_OPTS)


def _ensure_runner_deployed(host):
    """Copy the runner to `host` once per process, in a SINGLE ssh connection.

    We deliberately do NOT pre-check the remote version over its own connection:
    aggressive VPS SSH rate-limiters (fail2ban) ban an IP that opens several
    connections in quick succession, so every avoidable round-trip is a liability.
    One deploy per process (guarded by `_deployed_hosts`) plus the fetch itself is
    two connections total — gentle enough to stay unbanned."""
    if host in _deployed_hosts:
        return
    opts = _ssh_opts()
    with open(_RUNNER_LOCAL, "r", encoding="utf-8") as f:
        local_src = f.read()
    logger.info(f"[scrapling_stealth] deploying runner to {host}")
    # mkdir + write in one connection; stream the file over stdin (no scp needed).
    proc = subprocess.run(
        f"ssh {opts} {shlex.quote(host)} 'mkdir -p {REMOTE_RUNNER_DIR} && cat > {REMOTE_RUNNER_PATH}'",
        shell=True, input=local_src, capture_output=True, text=True, timeout=40,
    )
    if proc.returncode != 0:
        raise Exception(f"runner deploy to {host} failed: {(proc.stderr or '').strip()[:200]}")
    _deployed_hosts.add(host)


# SSH-transport hiccups worth a quiet retry rather than a failed fetch: an
# aggressive fail2ban briefly refusing new connections, or a reset mid-handshake.
_SSH_TRANSIENT = ("connection refused", "connection reset", "kex_exchange_identification",
                  "connection closed", "timed out", "operation timed out")


def _is_transient_ssh(msg):
    low = (msg or "").lower()
    return any(m in low for m in _SSH_TRANSIENT)


def _run_via_vps(host, remote_python, payload, timeout):
    """Execute one fetch on `host` using `remote_python`, returning the runner's
    parsed JSON response. Retries transient SSH-transport failures with backoff so
    a momentary fail2ban refusal doesn't sink an otherwise-good fetch."""
    _ensure_runner_deployed(host)
    opts = _ssh_opts()
    # SSH has to outlive the fetch itself (Camoufox + Cloudflare solve can take 120s+).
    ssh_timeout = max(timeout, 120) + 60
    attempts = int(_env("CDIO_STEALTH_SSH_RETRIES", "3"))
    last = None
    for attempt in range(attempts):
        proc = subprocess.run(
            f"ssh {opts} {shlex.quote(host)} {shlex.quote(remote_python)} {REMOTE_RUNNER_PATH}",
            shell=True, input=json.dumps(payload), capture_output=True, text=True,
            timeout=ssh_timeout,
        )
        if proc.returncode == 0:
            out = (proc.stdout or "").strip()
            if not out:
                raise Exception(f"empty response from {host} (stderr: {(proc.stderr or '').strip()[:200]})")
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                raise Exception(f"non-JSON response from {host}: {out[:300]}")
        last = f"ssh {host} exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        # ssh uses 255 for its own transport errors; only retry those, and only
        # when the message looks transient (a fail2ban refusal, a reset).
        if proc.returncode == 255 and _is_transient_ssh(proc.stderr) and attempt < attempts - 1:
            backoff = 5 * (attempt + 1)
            logger.info(f"[scrapling_stealth] transient ssh error to {host}, retry in {backoff}s ({attempt + 1}/{attempts})")
            time.sleep(backoff)
            continue
        break
    raise Exception(last or f"ssh {host} failed")


def _run_local(payload):
    """Run the same logic in-process (no SSH). Requires scrapling on the app host."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cdio_vps_runner", _RUNNER_LOCAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handle(payload)


class fetcher(Fetcher):
    fetcher_description = _l("Stealth (Scrapling/Camoufox via VPS egress — anti-bot)")

    # Capability flags — this backend fetches HTML/text and screenshots-of-image
    # content, but does not (yet) drive browser steps or the visual selector.
    supports_browser_steps = False
    supports_screenshots = False
    supports_xpath_element_data = False

    def __init__(self, proxy_override=None, custom_browser_connection_url=None, **kwargs):
        super().__init__(**kwargs)
        self.proxy_override = proxy_override

    @classmethod
    def get_status_icon_data(cls):
        return {
            'filename': 'Google-Chrome-icon.png',
            'alt': 'Stealth (Scrapling via VPS)',
            'title': 'Anti-bot fetch: Scrapling/Camoufox routed through a VPS egress IP',
            'style': 'height: 1em;',
        }

    def _ordered_hosts(self, url):
        """Egress hosts as (host, interpreter) tuples, ordered so the same target
        deterministically prefers one egress IP (warm cookies / consistency) while
        different targets fan out across the fleet. The rest follow as failover."""
        hosts = _vps_hosts()
        if not hosts:
            raise Exception("CDIO_STEALTH_VPS_HOSTS is empty and mode requires a VPS")
        h = int(hashlib.md5((url or "").encode("utf-8")).hexdigest(), 16)
        i = h % len(hosts)
        return [hosts[i]] + hosts[:i] + hosts[i + 1:]

    def _try_vps_fleet(self, ordered, payload, timeout):
        """Try each (host, interpreter) in turn; first success wins, else raise."""
        last_err = None
        for host, remote_python in ordered:
            try:
                resp = _run_via_vps(host, remote_python, payload, timeout)
                if resp.get("ok"):
                    resp["_egress"] = host
                    return resp
                last_err = resp.get("error")
                logger.warning(f"[scrapling_stealth] {host} runner error: {last_err}")
            except Exception as e:
                last_err = str(e)
                logger.warning(f"[scrapling_stealth] {host} failed: {e}")
        raise Exception(f"all VPS egress hosts failed: {last_err}")

    def _execute(self, payload, timeout):
        mode = _env("CDIO_STEALTH_MODE", "auto")

        if mode == "local":
            return _run_local(payload)

        ordered = self._ordered_hosts(payload["url"])

        if mode == "vps":
            return self._try_vps_fleet(ordered, payload, timeout)

        # mode == auto: local first (fast, no SSH), escalate to VPS on block/error.
        try:
            resp = _run_local({**payload, "tier": "static"})
            if resp.get("ok"):     # verdict == 'ok': real content, no challenge/empty
                resp["_egress"] = "local"
                return resp
            logger.info(f"[scrapling_stealth] local static verdict={resp.get('verdict')} "
                        f"({resp.get('antibot')}) — escalating to VPS egress")
        except Exception as e:
            logger.info(f"[scrapling_stealth] local fetch unavailable ({e}) — routing to VPS egress")

        return self._try_vps_fleet(ordered, payload, timeout)

    def _run_sync(self, url, timeout, request_headers, request_body, request_method,
                  ignore_status_codes=False, is_binary=False,
                  empty_pages_are_a_change=False):
        if self.browser_steps:
            raise BrowserStepsInUnsupportedFetcher(url=url)

        blocked = _ssrf_guard(url)
        if blocked:
            raise PageUnloadable(status_code=None, url=url, message=blocked)

        tier = _env("CDIO_STEALTH_TIER", "auto")
        payload = {
            "url": url,
            "tier": tier,
            "timeout": int(timeout or 60),
            "headers": request_headers or None,
            "method": (request_method or "GET").upper(),
            "body": request_body,
            "solve_cf": True,       # allow the runner to solve a challenge when it escalates
            "ignore_status": ignore_status_codes,
            # Residential proxy for datacenter-IP-flagging walls (DataDome/PerimeterX).
            # Per-watch proxy from changedetection.io wins; else the fleet-wide default.
            "proxy": self.proxy_override or _env("CDIO_STEALTH_RESIDENTIAL_PROXY", None),
        }

        resp = self._execute(payload, int(timeout or 60))

        verdict = resp.get("verdict")
        antibot = resp.get("antibot")

        # NEVER FALSE SILENCE: a challenge/blocked page is not content. Returning it
        # would make changedetection.io diff the challenge HTML and later report "no
        # change" while we're actually locked out. Raise instead, so the watch shows
        # an explicit error state ("access lost") the customer can see and act on.
        # Strict mode is on by default; set CDIO_STEALTH_STRICT=0 to return anyway.
        strict = _env("CDIO_STEALTH_STRICT", "1") != "0"
        if verdict == "challenge" and strict:
            wall = f"{antibot} " if antibot and antibot != "unknown" else ""
            raise PageUnloadable(
                status_code=resp.get("status"),
                url=url,
                message=(f"Access blocked by a {wall}anti-bot wall — the page could not be "
                         f"retrieved as real content. Monitoring is paused until access returns."),
            )

        if not resp.get("ok") and not (empty_pages_are_a_change and verdict == "empty"):
            if verdict == "empty":
                raise EmptyReply(url=url, status_code=resp.get("status"))
            raise Exception(resp.get("error") or f"stealth fetch failed (verdict={verdict})")

        self.status_code = int(resp.get("status") or 0)
        self.headers = resp.get("headers") or {}
        html = resp.get("html") or ""
        logger.debug(f"[scrapling_stealth] {url} -> {self.status_code} via {resp.get('_egress')} "
                     f"(tier {resp.get('tier_used')}, verdict {verdict}, {len(html)} bytes)")

        if not html:
            if not empty_pages_are_a_change:
                raise EmptyReply(url=url, status_code=self.status_code)
            logger.debug(f"[scrapling_stealth] {url} empty reply, status {self.status_code}, "
                         f"empty_pages_are_a_change=True")

        if self.status_code != 200 and not ignore_status_codes:
            raise Non200ErrorCodeReceived(url=url, status_code=self.status_code, page_html=html)

        self.raw_content = html.encode("utf-8")
        if is_binary:
            self.content = hashlib.md5(self.raw_content).hexdigest()
        else:
            self.content = html

    async def run(self, fetch_favicon=True, current_include_filters=None,
                  empty_pages_are_a_change=False, ignore_status_codes=False,
                  is_binary=False, request_body=None, request_headers=None,
                  request_method=None, screenshot_format=None, timeout=None,
                  url=None, watch_uuid=None):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._run_sync(
                url=url,
                timeout=timeout,
                request_headers=request_headers,
                request_body=request_body,
                request_method=request_method,
                ignore_status_codes=ignore_status_codes,
                is_binary=is_binary,
                empty_pages_are_a_change=empty_pages_are_a_change,
            ),
        )

    async def quit(self, watch=None):
        return

    def get_error(self):
        return self.error

    def get_last_status_code(self):
        return self.status_code

    def screenshot_step(self, step_n):
        return None

    def is_ready(self):
        # local mode needs scrapling here; vps/auto need at least one reachable host.
        mode = _env("CDIO_STEALTH_MODE", "auto")
        if mode == "local":
            import importlib.util
            return importlib.util.find_spec("scrapling") is not None
        return bool(_vps_hosts())

    def validate(self, url, timeout=90):
        """Prove-before-promise: fetch `url` through the full pipeline and return a
        structured verdict WITHOUT raising, so the add-watch flow can tell a customer
        up front whether we can actually watch this page — and if not, exactly why.

        Returns a dict:
          {watchable: bool, verdict: ok|challenge|empty|http_error|error,
           antibot: cloudflare|datadome|..|None, status, bytes, tier_used, egress,
           message: human-readable}
        Never trusts a challenge page as success.
        """
        blocked = _ssrf_guard(url)
        if blocked:
            return {"watchable": False, "verdict": "refused", "antibot": None, "status": 0,
                    "bytes": 0, "tier_used": None, "egress": None, "message": blocked}
        payload = {
            "url": url, "tier": _env("CDIO_STEALTH_TIER", "auto"),
            "timeout": int(timeout), "solve_cf": True,
            "proxy": self.proxy_override or _env("CDIO_STEALTH_RESIDENTIAL_PROXY", None),
        }
        try:
            resp = self._execute(payload, int(timeout))
        except Exception as e:
            return {"watchable": False, "verdict": "error", "antibot": None, "status": 0,
                    "bytes": 0, "tier_used": None, "egress": None,
                    "message": f"Could not reach the page: {str(e)[:160]}"}

        verdict = resp.get("verdict")
        antibot = resp.get("antibot")
        html_len = len(resp.get("html") or "")
        if verdict == "ok":
            msg = f"✅ Watchable — retrieved {html_len:,} bytes via {resp.get('tier_used')}."
        elif verdict == "challenge":
            wall = antibot if antibot and antibot != "unknown" else "an anti-bot"
            hard = antibot in ("datadome", "perimeterx", "akamai", "imperva")
            msg = (f"⚠️ Blocked by {wall}. "
                   + ("This wall flags datacenter IPs — a residential proxy is required to watch it reliably."
                      if hard else "We can usually solve this; retrying on a schedule may clear it."))
        elif verdict == "empty":
            msg = "⚠️ The page returned almost no content — likely a JS shell we couldn't render or a soft block."
        else:
            msg = f"⚠️ HTTP {resp.get('status')} — the page did not return usable content."
        return {
            "watchable": verdict == "ok", "verdict": verdict, "antibot": antibot,
            "status": resp.get("status"), "bytes": html_len,
            "tier_used": resp.get("tier_used"), "egress": resp.get("_egress"),
            "message": msg,
        }


def _cli():
    """Ad-hoc reachability probe:  python -m ...scrapling_stealth <url> [url2 ...]
    Prints the per-URL verdict — the same 'can we watch this?' check the add-watch
    flow uses, handy for measuring success rates across a target list."""
    import sys
    urls = sys.argv[1:]
    if not urls:
        print("usage: python -m changedetectionio.content_fetchers.scrapling_stealth <url> [...]")
        return
    f = fetcher()
    for u in urls:
        v = f.validate(u)
        print(f"{u}\n  {v['message']}  [verdict={v['verdict']} antibot={v['antibot']} "
              f"tier={v['tier_used']} egress={v['egress']}]")


class ScraplingStealthFetcherPlugin:
    """Registers the stealth fetcher via the changedetection.io pluggy hook."""

    def register_content_fetcher(self):
        return ('html_scrapling_stealth', fetcher)


scrapling_stealth_plugin = ScraplingStealthFetcherPlugin()


if __name__ == "__main__":
    _cli()
