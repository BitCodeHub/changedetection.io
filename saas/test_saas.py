"""
Smoke test for the SaaS control plane — runs entirely in mock/stub mode (no Docker,
no Stripe keys), exercising the full flow end to end:

  signup -> free instance provisioned -> upgrade to Pro via checkout stub ->
  subscription active -> tenant re-provisioned with Pro env -> quota env correct ->
  Stripe webhook (subscription.deleted) -> downgraded to free.

Run:  python -m saas.test_saas
"""
import os
import tempfile

# Isolate a throwaway DB + force mock/stub backends BEFORE importing the app.
os.environ["SAAS_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "saas_test.db")
os.environ["SAAS_PROVISIONER"] = "mock"
os.environ.pop("STRIPE_SECRET_KEY", None)          # -> billing stub mode
os.environ["SAAS_SECRET_KEY"] = "test-secret"
os.environ["SAAS_DOMAIN"] = "watch.test"

from . import models, provisioner, billing              # noqa: E402
from .plans import tenant_env_for_plan                   # noqa: E402
from .app import app                                     # noqa: E402


def run():
    client = app.test_client()

    # 1) signup
    r = client.post("/signup", data={"email": "sam@example.com", "password": "hunter2hunter2"},
                    follow_redirects=True)
    assert r.status_code == 200, r.status_code
    acc = models.get_account_by_email("sam@example.com")
    assert acc, "account not created"
    tenant = models.get_tenant(acc["id"])
    assert tenant and tenant["status"] == "running", f"tenant not provisioned: {tenant}"
    assert tenant["public_url"].endswith(".watch.test"), tenant["public_url"]
    sub = models.get_subscription(acc["id"])
    assert sub["plan"] == "free" and sub["status"] == "active", sub
    print(f"✓ signup: account {acc['id']}, tenant {tenant['slug']} running at {tenant['public_url']}, plan=free")

    # 2) free plan quota env
    env = tenant_env_for_plan("free")
    assert env["MAX_WATCHES"] == "5", env
    assert "CDIO_STEALTH_MODE" not in env, "free should not get stealth"
    print(f"✓ free env: MAX_WATCHES={env['MAX_WATCHES']}, stealth off")

    # 3) upgrade to Pro via the stub checkout (mimics Stripe returning success)
    r = client.post("/subscribe/pro", follow_redirects=False)
    assert r.status_code in (302, 303), r.status_code
    # stub checkout URL -> follow it to complete "payment"
    loc = r.headers["Location"]
    assert "stub-complete" in loc, loc
    r = client.get(loc.split("http://localhost:8099", 1)[-1] if loc.startswith("http") else loc,
                   follow_redirects=True)
    assert r.status_code == 200
    sub = models.get_subscription(acc["id"])
    assert sub["plan"] == "pro" and sub["status"] == "active", sub
    tenant = models.get_tenant(acc["id"])
    assert tenant["status"] == "running", tenant
    print(f"✓ upgrade: plan=pro active, tenant re-provisioned ({tenant['container_id']})")

    # 4) Pro quota env now includes stealth + higher cap
    penv = tenant_env_for_plan("pro")
    assert penv["MAX_WATCHES"] == "500", penv
    assert penv.get("CDIO_STEALTH_MODE"), "pro should enable stealth"
    print(f"✓ pro env: MAX_WATCHES={penv['MAX_WATCHES']}, stealth={penv['CDIO_STEALTH_MODE']}")

    # 5) Stripe webhook: subscription canceled -> downgrade to free
    # (re-fetch: the Stripe customer id was assigned during the upgrade above)
    acc = models.get_account(acc["id"])
    assert acc["stripe_customer_id"], "customer id should be set after checkout"
    import json
    payload = json.dumps({
        "type": "customer.subscription.deleted",
        "data": {"object": {"customer": acc["stripe_customer_id"]}},
    }).encode()
    ok, msg = billing.handle_webhook(payload, "")
    assert ok, msg
    sub = models.get_subscription(acc["id"])
    assert sub["plan"] == "free" and sub["status"] == "canceled", sub
    print(f"✓ webhook cancel: {msg}")

    # 6) healthz reflects modes
    h = client.get("/healthz").get_json()
    assert h["provisioner"] == "mock" and h["billing"] == "stub", h
    print(f"✓ healthz: {h}")

    print("\nALL SAAS SMOKE TESTS PASSED")


if __name__ == "__main__":
    run()
