"""
Tenant provisioning — one isolated changedetection.io container per account.

Container-per-tenant is the model here: complete data isolation with zero changes
to the changedetection.io core, and the plan's quota/interval/stealth settings are
just the container's environment (see plans.tenant_env_for_plan).

Two backends, chosen by SAAS_PROVISIONER:
  docker  — real containers via the Docker SDK, each with its own named volume and
            a Traefik label so <slug>.<SAAS_DOMAIN> routes to it.
  mock    — no Docker; records a fake container id + url. Lets the whole signup →
            billing → provision → quota flow run and be tested on a laptop.

Reprovisioning (plan change) recreates the container with new env — the data volume
is keyed by slug and preserved across recreate, so a user keeps their watches.
"""
import os
import time

from . import models
from .plans import tenant_env_for_plan, get_plan

PROVISIONER = os.getenv("SAAS_PROVISIONER", "mock")
IMAGE = os.getenv("SAAS_TENANT_IMAGE", "ghcr.io/dgtlmoon/changedetection.io:latest")
SAAS_DOMAIN = os.getenv("SAAS_DOMAIN", "watch.example.com")
# A shared Playwright/Chrome endpoint all tenants use for the browser fetch tier.
PLAYWRIGHT_URL = os.getenv("PLAYWRIGHT_DRIVER_URL", "")
DOCKER_NETWORK = os.getenv("SAAS_DOCKER_NETWORK", "cdio_saas")


def _public_url(slug):
    return f"https://{slug}.{SAAS_DOMAIN}"


def _container_name(slug):
    return f"cdio-tenant-{slug}"


def _volume_name(slug):
    return f"cdio-data-{slug}"


def _tenant_env(account_id, plan_id):
    env = tenant_env_for_plan(plan_id)
    env["BASE_URL"] = _public_url(models.get_tenant(account_id)["slug"])
    if PLAYWRIGHT_URL:
        env["PLAYWRIGHT_DRIVER_URL"] = PLAYWRIGHT_URL
    # Propagate operator-level config from the control plane's environment to every
    # tenant: the LLM the AI analyst uses, and the stealth gateway credentials.
    # Without the LLM vars an Analyst tenant has the agent flag but nowhere to think.
    for key in ("LLM_MODEL", "LLM_API_KEY", "LLM_API_BASE", "LLM_TIMEOUT",
                "LLM_LOCAL_TIMEOUT", "CDIO_STEALTH_GATEWAY", "CDIO_STEALTH_GATEWAY_TOKEN",
                "ALLOW_IANA_RESTRICTED_ADDRESSES"):
        val = os.getenv(key)
        if val is not None and key not in env:
            env[key] = val
    return env


# ── mock backend ──────────────────────────────────────────────────────────────
def _provision_mock(account_id, slug, plan_id):
    return {
        "container_id": f"mock-{slug}-{int(time.time())}",
        "internal_url": f"http://{_container_name(slug)}:5000",
        "public_url": _public_url(slug),
    }


# ── docker backend ────────────────────────────────────────────────────────────
def _docker_client():
    import docker
    return docker.from_env()


def _provision_docker(account_id, slug, plan_id):
    client = _docker_client()
    name = _container_name(slug)
    env = _tenant_env(account_id, plan_id)

    # Remove a previous container with this name (plan change / reprovision); the
    # named data volume is NOT removed, so the tenant keeps its watches.
    try:
        old = client.containers.get(name)
        old.remove(force=True)
    except Exception:
        pass

    # Ensure the shared network exists so Traefik can reach the container.
    try:
        client.networks.get(DOCKER_NETWORK)
    except Exception:
        client.networks.create(DOCKER_NETWORK, driver="bridge")

    # Entrypoint/TLS are configurable so a dev/staging box (no domain, no ACME) can
    # route on plain HTTP, while production uses websecure + Let's Encrypt.
    entrypoint = os.getenv("SAAS_TRAEFIK_ENTRYPOINT", "websecure")
    certresolver = os.getenv("SAAS_TRAEFIK_CERTRESOLVER", "le")
    labels = {
        "traefik.enable": "true",
        f"traefik.http.routers.{name}.rule": f"Host(`{slug}.{SAAS_DOMAIN}`)",
        f"traefik.http.routers.{name}.entrypoints": entrypoint,
        f"traefik.http.services.{name}.loadbalancer.server.port": "5000",
    }
    # Only attach TLS on a TLS entrypoint with a resolver (production).
    if entrypoint == "websecure" and certresolver:
        labels[f"traefik.http.routers.{name}.tls.certresolver"] = certresolver

    container = client.containers.run(
        IMAGE,
        name=name,
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        environment=env,
        volumes={_volume_name(slug): {"bind": "/datastore", "mode": "rw"}},
        network=DOCKER_NETWORK,
        # Let a tenant resolve the Docker host (for host-run services like a local
        # LLM endpoint). Harmless in cloud where nothing points at it.
        extra_hosts={"host.docker.internal": "host-gateway"},
        labels=labels,
    )
    return {
        "container_id": container.id,
        "internal_url": f"http://{name}:5000",
        "public_url": _public_url(slug),
    }


def provision(account_id, plan_id):
    """Create (or recreate) the tenant's instance and record it. Idempotent per slug."""
    tenant = models.get_tenant(account_id)
    slug = tenant["slug"]
    backend = _provision_docker if PROVISIONER == "docker" else _provision_mock
    info = backend(account_id, slug, plan_id)
    models.update_tenant(
        account_id,
        container_id=info["container_id"],
        internal_url=info["internal_url"],
        public_url=info["public_url"],
        status="running",
    )
    return info


def deprovision(account_id):
    """Stop/remove the tenant container (e.g. subscription canceled and grace over).
    The data volume is retained so a returning customer keeps their watches."""
    tenant = models.get_tenant(account_id)
    if not tenant:
        return
    if PROVISIONER == "docker":
        try:
            _docker_client().containers.get(_container_name(tenant["slug"])).remove(force=True)
        except Exception:
            pass
    models.update_tenant(account_id, status="suspended", container_id=None)


def apply_plan(account_id, plan_id):
    """Reflect a plan change: recreate the container with the new plan's env so
    MAX_WATCHES / interval / stealth settings take effect."""
    return provision(account_id, plan_id)
