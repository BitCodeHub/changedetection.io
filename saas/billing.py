"""
Stripe billing for the control plane.

Kept thin and env-keyed so the same code runs against a Stripe test account and a
live one with no edits. When STRIPE_SECRET_KEY is absent the module runs in a
local "stub" mode: checkout returns a fake URL and webhook signatures are not
verified, so the end-to-end flow (signup → checkout → activate → provision) can be
exercised on a laptop without touching Stripe.

Flow:
  create_checkout_session()  -> a Stripe Checkout URL for a plan
  handle_webhook()           -> maps checkout.session.completed / customer.subscription.*
                                events onto our subscription table + (re)provisions

The webhook is the source of truth for entitlement — never trust the client's
"I paid" redirect.
"""
import json
import os
import time

from . import models
from .plans import get_plan, plan_from_stripe_price, PLANS

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SAAS_BASE_URL = os.getenv("SAAS_BASE_URL", "http://localhost:8099")
STUB = not STRIPE_SECRET_KEY


def _stripe():
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def ensure_customer(account):
    """Return the account's Stripe customer id, creating it on first use."""
    if account.get("stripe_customer_id"):
        return account["stripe_customer_id"]
    if STUB:
        cid = f"cus_stub_{account['id']}"
    else:
        cid = _stripe().Customer.create(email=account["email"]).id
    models.set_stripe_customer(account["id"], cid)
    return cid


def create_checkout_session(account, plan_id):
    """Create a subscription Checkout session for `plan_id`. Returns a URL to send
    the browser to. In stub mode returns a local URL that simulates success."""
    plan = get_plan(plan_id)
    if plan["price_usd"] == 0:
        raise ValueError("free plan needs no checkout")

    customer_id = ensure_customer(account)

    if STUB:
        # Simulate Stripe: the "success" page will call our own activation directly.
        return f"{SAAS_BASE_URL}/billing/stub-complete?account_id={account['id']}&plan={plan_id}"

    price_id = plan.get("stripe_price_id")
    if not price_id:
        raise ValueError(f"plan '{plan_id}' has no STRIPE_PRICE_* configured")

    session = _stripe().checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{SAAS_BASE_URL}/dashboard?checkout=success",
        cancel_url=f"{SAAS_BASE_URL}/dashboard?checkout=cancel",
        metadata={"account_id": account["id"], "plan": plan_id},
        subscription_data={"metadata": {"account_id": account["id"], "plan": plan_id}},
    )
    return session.url


def _activate(account_id, plan_id, subscription_id=None, period_end=None):
    """Set the subscription active on our side and (re)provision the tenant so the
    new plan's quota/interval/stealth env is live. This is the one place entitlement
    is granted."""
    models.set_subscription(
        account_id,
        plan=plan_id,
        status="active",
        stripe_subscription_id=subscription_id,
        current_period_end=period_end,
    )
    from . import provisioner
    provisioner.apply_plan(account_id, plan_id)


def stub_complete(account_id, plan_id):
    """Local-only: called by the stub success page to mimic a paid subscription."""
    if not STUB:
        raise RuntimeError("stub_complete is only valid without Stripe keys")
    _activate(int(account_id), plan_id, subscription_id=f"sub_stub_{account_id}",
              period_end=int(time.time()) + 30 * 86400)


def assert_config():
    """Fail fast on a dangerous misconfiguration: a live Stripe key with no webhook
    secret would mean the webhook — our ONLY source of entitlement — could not be
    verified, so anyone could POST a forged 'you paid' event. Called at app startup."""
    if not STUB and not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET must be set whenever STRIPE_SECRET_KEY is set — "
            "refusing to start with unverifiable webhooks.")


def _verify_and_parse(payload, sig_header):
    # STUB mode (no Stripe key at all) is the only path that skips verification, and
    # it is unreachable in production because the /webhooks/stripe route only matters
    # once a real key is configured. With a real key, verification is MANDATORY —
    # never fall back to trusting an unsigned payload.
    if STUB:
        return json.loads(payload)
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is required to verify webhooks")
    return _stripe().Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)


def handle_webhook(payload, sig_header):
    """Process a Stripe webhook. Returns (ok, message)."""
    try:
        event = _verify_and_parse(payload, sig_header)
    except Exception as e:
        return False, f"signature/parse error: {e}"

    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        account_id = (obj.get("metadata") or {}).get("account_id")
        plan_id = (obj.get("metadata") or {}).get("plan")
        if account_id and plan_id:
            _activate(int(account_id), plan_id,
                      subscription_id=obj.get("subscription"))
            return True, f"activated account {account_id} -> {plan_id}"
        return False, "checkout.session.completed missing metadata"

    if etype in ("customer.subscription.updated", "customer.subscription.created"):
        account_id = (obj.get("metadata") or {}).get("account_id")
        if not account_id:
            acc = models.account_by_stripe_customer(obj.get("customer"))
            account_id = acc["id"] if acc else None
        items = (obj.get("items") or {}).get("data") or []
        price_id = items[0]["price"]["id"] if items else None
        plan_id = plan_from_stripe_price(price_id) if price_id else (obj.get("metadata") or {}).get("plan")
        if account_id and plan_id and obj.get("status") in ("active", "trialing"):
            _activate(int(account_id), plan_id,
                      subscription_id=obj.get("id"),
                      period_end=obj.get("current_period_end"))
            return True, f"subscription active: account {account_id} -> {plan_id}"
        return True, "subscription event noted (no activation)"

    if etype == "customer.subscription.deleted":
        acc = models.account_by_stripe_customer(obj.get("customer"))
        if acc:
            models.set_subscription(acc["id"], plan="free", status="canceled")
            from . import provisioner
            # downgrade to free plan env rather than kill the container outright
            provisioner.apply_plan(acc["id"], "free")
            return True, f"downgraded account {acc['id']} to free"
        return False, "subscription.deleted: unknown customer"

    return True, f"ignored event {etype}"
