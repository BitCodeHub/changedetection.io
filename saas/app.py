"""
Control-plane web app: signup, login, plan/billing, and the tenant dashboard.

This is the front door of the SaaS. It never serves changedetection.io itself —
each tenant runs in its own container (see provisioner) reached at its own
subdomain; this app manages accounts, subscriptions, and provisioning, and links
the user through to their instance.

Run:  python -m saas.app        (or gunicorn saas.app:app)
Env:  SAAS_SECRET_KEY, SAAS_BASE_URL, SAAS_DOMAIN, SAAS_PROVISIONER=docker|mock,
      STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET / STRIPE_PRICE_MONITOR / STRIPE_PRICE_ANALYST
"""
import hmac
import os
import re
import secrets
from urllib.parse import urlparse

from flask import (Flask, request, redirect, url_for, session, render_template,
                   flash, abort, Response)

from . import models, billing, provisioner
from .plans import PLANS, get_plan, DEFAULT_PLAN

app = Flask(__name__)
app.secret_key = os.getenv("SAAS_SECRET_KEY", secrets.token_hex(32))

# Session cookie hardening. Secure defaults to on and should only be turned off
# for local http testing (SAAS_INSECURE_COOKIES=1).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not os.getenv("SAAS_INSECURE_COOKIES"),
)

models.init_db()
billing.assert_config()   # refuse to start on an unverifiable-webhook misconfig


# ── CSRF protection ───────────────────────────────────────────────────────────
# Every state-changing POST from a browser must carry the session's CSRF token.
# The Stripe webhook is exempt: it is not a browser form and is authenticated by
# its Stripe signature instead.
CSRF_EXEMPT = {"/webhooks/stripe"}


def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_hex(16)
        session["_csrf"] = tok
    return tok


@app.before_request
def csrf_protect():
    if request.method == "POST" and request.path not in CSRF_EXEMPT:
        sent = request.form.get("_csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "CSRF token missing or invalid")


@app.context_processor
def inject_csrf():
    return {"csrf_token": _csrf_token()}

SLUG_RE = re.compile(r"[^a-z0-9]+")


def safe_next(target):
    """Only allow same-site relative redirects. Rejects absolute URLs
    (scheme/host) and protocol-relative '//host' targets, so `next` can't be used
    as an open-redirect into a phishing site."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _slug_for(email, account_id):
    base = SLUG_RE.sub("-", email.split("@")[0].lower()).strip("-") or "user"
    return f"{base}-{account_id}"


def current_account():
    aid = session.get("account_id")
    return models.get_account(aid) if aid else None


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapper(*a, **k):
        if not current_account():
            return redirect(url_for("login", next=request.path))
        return view(*a, **k)

    return wrapper


@app.route("/")
def index():
    return render_template("index.html", plans=PLANS, account=current_account())


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or "@" not in email or len(password) < 8:
            flash("Enter a valid email and a password of at least 8 characters.")
            return redirect(url_for("signup"))
        if models.get_account_by_email(email):
            flash("That email is already registered.")
            return redirect(url_for("login"))

        account_id = models.create_account(email, password)
        # every account gets a tenant record + a free-tier instance immediately
        slug = _slug_for(email, account_id)
        models.create_tenant(account_id, slug)
        provisioner.provision(account_id, DEFAULT_PLAN)

        session["account_id"] = account_id
        flash("Welcome! Your free instance is being set up.")
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        acc = models.get_account_by_email(email)
        if not acc or not models.verify_password(password, acc["pw_hash"], acc["pw_salt"]):
            flash("Wrong email or password.")
            return redirect(url_for("login"))
        session["account_id"] = acc["id"]
        return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    account = current_account()
    sub = models.get_subscription(account["id"])
    tenant = models.get_tenant(account["id"])
    plan = get_plan(sub["plan"] if sub else DEFAULT_PLAN)
    return render_template("dashboard.html", account=account, sub=sub, tenant=tenant,
                           plan=plan, plans=PLANS)


@app.route("/subscribe/<plan_id>", methods=["POST"])
@login_required
def subscribe(plan_id):
    account = current_account()
    if plan_id not in PLANS:
        abort(404)
    if get_plan(plan_id)["price_usd"] == 0:
        # downgrade to free is immediate
        models.set_subscription(account["id"], plan="free", status="active")
        provisioner.apply_plan(account["id"], "free")
        flash("Switched to the Free plan.")
        return redirect(url_for("dashboard"))
    try:
        checkout_url = billing.create_checkout_session(account, plan_id)
    except Exception as e:
        flash(f"Could not start checkout: {e}")
        return redirect(url_for("dashboard"))
    return redirect(checkout_url)


@app.route("/billing/stub-complete")
@login_required
def billing_stub_complete():
    """Local-only success page that mimics Stripe returning from checkout.

    Activates ONLY the logged-in account (never a client-supplied account_id), and
    only for a real paid plan, so even in dev this can't grant someone else a plan."""
    if not billing.STUB:
        abort(404)
    account = current_account()
    plan_id = request.args.get("plan")
    if plan_id not in PLANS or get_plan(plan_id)["price_usd"] == 0:
        abort(400)
    billing.stub_complete(account["id"], plan_id)
    flash(f"(stub) Subscription to {plan_id} activated.")
    return redirect(url_for("dashboard"))


@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    ok, msg = billing.handle_webhook(request.data, request.headers.get("Stripe-Signature", ""))
    return Response(msg, status=200 if ok else 400)


@app.route("/healthz")
def healthz():
    return {"ok": True, "provisioner": provisioner.PROVISIONER, "billing": "stub" if billing.STUB else "stripe"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("SAAS_PORT", "8099")), debug=bool(os.getenv("SAAS_DEBUG")))
