"""
Rivalore AI analyst agent.

This is what separates the Analyst tier from a raw diff monitor. When a change is
detected on a watched competitor page, the agent reads the before/after and produces
a short business brief — what changed, why it matters, and a suggested action —
instead of leaving the customer to interpret a red/green text diff.

It reuses changedetection.io's existing LLM client (`llm.client.completion`) and the
watch's configured LLM settings, so it runs on whatever the operator already set up —
including a local Ollama model, which keeps per-brief cost at zero.

Gating: only runs when the tenant is on the Analyst tier (RIVALORE_AI_AGENT=1, set by
the SaaS control plane) AND an LLM is configured. Both absent -> returns None and the
notification simply omits the {{ai_brief}} token.

Entry point:  build_brief(prev_text, current_text, url, title, llm_config) -> str | None
"""
import json
import os
import re

from loguru import logger

# Keep the model's input bounded — a huge diff wastes tokens and time. We send a
# trimmed before/after window; the brief only needs the substance of the change.
MAX_CHARS_PER_SIDE = 6000

SYSTEM_PROMPT = (
    "You are a competitive-intelligence analyst. You are given the BEFORE and AFTER "
    "text of a competitor's web page that just changed. Identify what materially "
    "changed and explain it for a busy business owner.\n"
    "\n"
    "Rules:\n"
    "- Ignore cosmetic/boilerplate noise: nav, cookie banners, timestamps, copyright "
    "years, customer counts, 'trusted by' numbers, legal footers.\n"
    "- Never invent facts not supported by the text. Quote concrete specifics (exact "
    "prices, plan names, role titles, feature names) when they appear.\n"
    "- headline: ONE sentence, specific and concrete — include the actual numbers or "
    "names that changed (e.g. 'Acme cut Pro from $50 to $39/mo and dropped its free "
    "trial'), not a vague summary like 'restructured its pricing'.\n"
    "- what_changed: the factual before->after, one or two sentences.\n"
    "- why_it_matters: the business implication for a competitor, ONE sentence.\n"
    "- suggested_action: one concrete next step, ONE sentence.\n"
    "- category — choose the SINGLE best fit:\n"
    "    pricing = prices, plans, tiers, discounts, trials\n"
    "    product = features, integrations, launches, roadmap, deprecations\n"
    "    messaging = positioning, tagline, target audience, value proposition\n"
    "    hiring = ANY change to careers/jobs/open-roles pages (new roles signal strategy)\n"
    "    legal = terms, privacy, compliance, security/certification badges\n"
    "    availability = stock, restock, sold-out, regional/geo availability\n"
    "    other = only if none of the above clearly fit\n"
    "- materiality: high = pricing/product/positioning moves or strategic hiring; "
    "medium = notable but not urgent; low = cosmetic or administrative only. If nothing "
    "material changed, set materiality 'low' and say so plainly.\n"
    "\n"
    "Respond ONLY with a JSON object: "
    '{"headline": "...", "what_changed": "...", "why_it_matters": "...", '
    '"suggested_action": "...", "category": "pricing|product|messaging|hiring|'
    'legal|availability|other", "materiality": "low|medium|high"}'
)


def is_enabled():
    """Analyst tier gate. The control plane sets RIVALORE_AI_AGENT=1 on Analyst tenants."""
    return os.getenv("RIVALORE_AI_AGENT", "0") == "1"


def _trim(text, limit=MAX_CHARS_PER_SIDE):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]…"


def _parse_json(raw):
    """Pull the JSON object out of the model's reply, tolerating stray prose/fences."""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _format(brief, url):
    """Render the structured brief into the plain-text block used in notifications."""
    mat = str(brief.get("materiality", "")).lower()
    badge = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "⚪ LOW"}.get(mat, "•")
    cat = str(brief.get("category", "other")).lower()
    lines = [
        f"🛰️ Rivalore Analyst — {badge} impact · {cat}",
        "",
        brief.get("headline", "").strip(),
    ]
    if brief.get("what_changed"):
        lines += ["", f"What changed: {brief['what_changed'].strip()}"]
    if brief.get("why_it_matters"):
        lines += [f"Why it matters: {brief['why_it_matters'].strip()}"]
    if brief.get("suggested_action"):
        lines += [f"Suggested action: {brief['suggested_action'].strip()}"]
    return "\n".join(l for l in lines if l is not None)


def _cfg(llm_config, key):
    """Read a field from the LLM config, which the app passes as a dict
    (get_llm_config returns {'model','api_key','api_base'}) but callers/tests may
    pass as an object. Support both."""
    if llm_config is None:
        return None
    if isinstance(llm_config, dict):
        return llm_config.get(key)
    return getattr(llm_config, key, None)


def build_brief(prev_text, current_text, url="", title="", llm_config=None):
    """Produce a business-language brief for a detected change, or None.

    Returns None (and logs) rather than raising, so a brief failure never blocks the
    underlying change notification — the customer still gets the raw diff.
    """
    if not is_enabled():
        return None
    model = _cfg(llm_config, "model")
    if not model:
        logger.debug("[rivalore] AI agent enabled but no LLM configured — skipping brief")
        return None
    if not (current_text or "").strip():
        return None

    try:
        from changedetectionio.llm.client import completion
    except Exception as e:
        logger.warning(f"[rivalore] LLM client unavailable: {e}")
        return None

    user = (
        f"Competitor page: {title or url}\nURL: {url}\n\n"
        f"=== BEFORE ===\n{_trim(prev_text)}\n\n"
        f"=== AFTER ===\n{_trim(current_text)}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

    # max_tokens headroom: gemma pretty-prints fenced JSON, which truncated (and so
    # failed to parse) at 500 tokens. For Ollama, also force native JSON mode so the
    # reply is clean JSON with no ``` fences and no leading prose — kept Ollama-only
    # so an OpenAI/Anthropic-configured tenant (which uses response_format) is
    # unaffected.
    kwargs = {"max_tokens": 1200}
    if "ollama" in (model or "").lower():
        kwargs["extra_body"] = {"format": "json"}

    try:
        text, *_ = completion(
            model=model,
            messages=messages,
            api_key=_cfg(llm_config, "api_key"),
            api_base=_cfg(llm_config, "api_base"),
            **kwargs,
        )
    except Exception as e:
        logger.warning(f"[rivalore] analyst LLM call failed for {url}: {e}")
        return None

    brief = _parse_json(text)
    if not brief or not brief.get("headline"):
        logger.debug(f"[rivalore] analyst produced no usable brief for {url}")
        return None

    logger.info(f"[rivalore] analyst brief for {url}: "
                f"{brief.get('materiality')}/{brief.get('category')} — {brief.get('headline')[:60]}")
    return _format(brief, url)
