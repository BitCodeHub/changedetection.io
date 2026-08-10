"""
Plan catalog for the SaaS control plane.

One place defines what each tier costs, how many watches it allows, the minimum
check interval, and whether the stealth (Scrapling-via-VPS) fetcher is on. The
control plane reads these to (a) render pricing, (b) create the right Stripe
price at checkout, and (c) stamp the tenant container's environment
(MAX_WATCHES, MINIMUM_SECONDS_RECHECK_TIME, the stealth-fetcher vars).

`stripe_price_id` is filled from the environment so the same code works across
test and live Stripe accounts without edits.
"""
import os

PLANS = {
    "free": {
        "name": "Free",
        "price_usd": 0,
        "max_watches": 5,
        "min_interval_seconds": 10800,   # 3h — gentle on the shared fetch fleet
        "stealth_fetcher": False,
        "stripe_price_id": None,         # no charge, no Stripe price
        "description": "5 watches, 3-hour checks, standard fetch.",
    },
    "pro": {
        "name": "Pro",
        "price_usd": 8.99,
        "max_watches": 500,
        "min_interval_seconds": 900,     # 15m
        "stealth_fetcher": True,
        "stripe_price_id": os.getenv("STRIPE_PRICE_PRO"),
        "description": "500 watches, 15-minute checks, anti-bot stealth fetching.",
    },
    "business": {
        "name": "Business",
        "price_usd": 24.99,
        "max_watches": 5000,
        "min_interval_seconds": 180,     # 3m
        "stealth_fetcher": True,
        "stripe_price_id": os.getenv("STRIPE_PRICE_BUSINESS"),
        "description": "5,000 watches, 3-minute checks, anti-bot stealth fetching, priority fleet.",
    },
}

DEFAULT_PLAN = "free"
PAID_PLANS = [k for k, v in PLANS.items() if v["price_usd"] > 0]


def get_plan(plan_id):
    return PLANS.get(plan_id or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def plan_from_stripe_price(price_id):
    """Reverse-map a Stripe price id back to our plan key (webhook handling)."""
    for key, plan in PLANS.items():
        if plan.get("stripe_price_id") and plan["stripe_price_id"] == price_id:
            return key
    return None


def tenant_env_for_plan(plan_id):
    """The environment a tenant container should run with for this plan.

    MAX_WATCHES and MINIMUM_SECONDS_RECHECK_TIME are read by the changedetection.io
    core; the CDIO_STEALTH_* vars switch the stealth fetcher on for paid tiers.
    """
    plan = get_plan(plan_id)
    env = {
        "MAX_WATCHES": str(plan["max_watches"]),
        "MINIMUM_SECONDS_RECHECK_TIME": str(plan["min_interval_seconds"]),
    }
    if plan["stealth_fetcher"]:
        env.update({
            "CDIO_STEALTH_MODE": os.getenv("CDIO_STEALTH_MODE", "auto"),
            "CDIO_STEALTH_VPS_HOSTS": os.getenv("CDIO_STEALTH_VPS_HOSTS", "nexa-vps"),
            "CDIO_STEALTH_TIER": os.getenv("CDIO_STEALTH_TIER", "auto"),
        })
    return env
