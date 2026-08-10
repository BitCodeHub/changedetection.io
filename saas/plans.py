"""
Plan catalog for Rivalore — competitive-intelligence monitoring.

Two paid products, exactly the split the business is sold on:
  Monitor  (non-AI) — reliable change alerts with stealth anti-bot reach.
  Analyst  (AI)     — everything in Monitor, plus an autonomous AI agent that
                      interprets each change and reports back in plain business
                      language ("Acme dropped their price 15% and killed the free
                      tier — they're moving upmarket").

One place defines what each tier costs, how many competitor pages it can watch,
the minimum check interval, whether stealth fetching is on, and whether the AI
analyst agent runs. The control plane reads these to (a) render pricing, (b) pick
the Stripe price at checkout, and (c) stamp the tenant container's environment.

`stripe_price_id` comes from the environment so the same code runs against a
Stripe test account and a live one with no edits.
"""
import os

PLANS = {
    "free": {
        "name": "Free",
        "price_usd": 0,
        "max_watches": 5,
        "min_interval_seconds": 10800,   # 3h — gentle on the shared fetch fleet
        "stealth_fetcher": False,
        "ai_agent": False,
        "stripe_price_id": None,
        "description": "5 competitor pages, 3-hour checks, standard fetch. Kick the tires free.",
    },
    "monitor": {
        "name": "Monitor",
        "price_usd": 39,
        "max_watches": 200,
        "min_interval_seconds": 900,     # 15m
        "stealth_fetcher": True,
        "ai_agent": False,
        "stripe_price_id": os.getenv("STRIPE_PRICE_MONITOR"),
        "description": "200 pages, 15-minute checks, anti-bot stealth reach. Alerts when a rival's page changes.",
    },
    "analyst": {
        "name": "Analyst",
        "price_usd": 149,
        "max_watches": 1000,
        "min_interval_seconds": 300,     # 5m
        "stealth_fetcher": True,
        "ai_agent": True,
        "stripe_price_id": os.getenv("STRIPE_PRICE_ANALYST"),
        "description": "1,000 pages, 5-minute checks, stealth reach, and an AI analyst that reads every change and briefs you on what it means.",
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
    core; the CDIO_STEALTH_* vars switch the stealth fetcher on for paid tiers; and
    RIVALORE_AI_AGENT turns on the AI analyst reporting agent for the Analyst tier.
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
    if plan["ai_agent"]:
        env["RIVALORE_AI_AGENT"] = "1"
    return env
