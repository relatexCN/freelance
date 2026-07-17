"""Explainable metadata-based model scoring and ranking."""

import logging
import math
import re
import time
from typing import Optional

logger = logging.getLogger("freelance.scoring")

# Fallback metadata for upstreams that omit context length or creation time.
MODEL_SPECS = {
    "llama-3.3": (128000, 0.80, 1733529600),
    "llama-3.2": (128000, 0.70, 1727308800),
    "llama-3.1": (128000, 0.75, 1721692800),
    "llama-3": (8192, 0.50, 1713484800),
    "gpt-4o": (128000, 0.85, 1715558400),
    "gpt-4.1": (128000, 0.90, 1730000000),
    "o1": (128000, 0.90, 1725000000),
    "gemini-1.5-pro": (1000000, 0.80, 1715558400),
    "gemini-1.5-flash": (1000000, 0.65, 1715558400),
    "gemini-2.0-flash": (1000000, 0.70, 1734000000),
    "qwen2.5": (128000, 0.75, 1726704000),
    "deepseek-v3": (128000, 0.85, 1735000000),
    "deepseek-r1": (128000, 0.90, 1737000000),
}

# Capabilities are scored by useful feature groups.  Counting every parameter
# independently over-rewards APIs that expose many equivalent sampling knobs.
CAPABILITY_GROUPS = {
    "tools": (0.25, {"tools", "tool_choice", "parallel_tool_calls"}),
    "reasoning": (0.25, {"reasoning", "reasoning_effort", "include_reasoning"}),
    "structured_output": (0.20, {"structured_outputs", "response_format", "json_mode"}),
    "token_control": (0.10, {"max_tokens", "max_completion_tokens", "stop"}),
    "sampling": (0.08, {"temperature", "top_p", "top_k", "min_p"}),
    "penalties": (0.06, {"frequency_penalty", "presence_penalty", "repetition_penalty", "logit_bias"}),
    "observability": (0.06, {"logprobs", "top_logprobs", "seed"}),
}

PREMIUM_TIER_MARKERS = ("pro", "ultra", "max", "opus", "sonnet", "large")
FAST_TIER_MARKERS = ("flash", "lite", "mini", "nano", "small")
UNSTABLE_MARKERS = ("preview", "beta", "experimental", "nightly", "canary")


def heuristically_infer_specs(model_id: str):
    """Guess missing context/capability/recency metadata from a model ID."""
    normalized_id = model_id.lower()
    context, capability, released = 8192, 0.40, 0.0
    for key, specs in MODEL_SPECS.items():
        if key in normalized_id:
            context, capability, released = specs
            break
    return context, capability, released


def _context_score(context_length: int) -> float:
    """Use diminishing returns between 8K and 1M tokens."""
    lower, upper = 8192, 1_000_000
    bounded = min(max(context_length, lower), upper)
    return math.log(bounded / lower) / math.log(upper / lower)


def _capability_score(model: dict, inferred_score: float) -> float:
    raw = model.get("supported_parameters") or model.get("capabilities") or []
    if not isinstance(raw, (list, tuple, set)) or not raw:
        return min(max(inferred_score, 0.0), 1.0)
    capabilities = {str(item).strip().lower() for item in raw}
    return sum(
        weight for weight, members in CAPABILITY_GROUPS.values()
        if capabilities.intersection(members)
    )


def _recency_score(created: float, half_life_days: int, now: float) -> float:
    """Smoothly decay age; invalid or missing timestamps receive a neutral score."""
    earliest_supported = 1420070400  # 2015-01-01
    if not created or created < earliest_supported or created > now + 7 * 86400:
        return 0.50
    age_days = max(0.0, (now - created) / 86400)
    return 2 ** (-age_days / half_life_days)


def _model_tier_score(model_id: str) -> float:
    name = model_id.lower().replace("_", "-")
    tokens = {token for token in re.split(r"[^a-z0-9]+", name) if token}
    if tokens.intersection(PREMIUM_TIER_MARKERS) or re.search(r"(?:^|[-/])(70|120|405|550)b(?:$|[-:])", name):
        return 1.0
    if tokens.intersection(FAST_TIER_MARKERS):
        return 0.50
    return 0.75


def _stability_score(model_id: str) -> float:
    name = model_id.lower()
    if any(marker in name for marker in UNSTABLE_MARKERS):
        return 0.45
    if "latest" in name or name.startswith("~"):
        return 0.70
    return 1.0


def calculate_model_score(model: dict, now: Optional[float] = None) -> float:
    """Calculate and attach an explainable score between 0 and 1."""
    from app.config import config

    scoring = config.settings.scoring
    model_id = str(model.get("id", "")).lower()
    inferred_context, inferred_capability, inferred_release = heuristically_infer_specs(model_id)

    try:
        context_length = int(model.get("context_length", 0)) or inferred_context
    except (TypeError, ValueError):
        context_length = inferred_context
    model["context_length"] = context_length

    created = model.get("created")
    if not isinstance(created, (int, float)):
        created = inferred_release
    current_time = time.time() if now is None else now

    raw_components = {
        "capabilities": _capability_score(model, inferred_capability),
        "context": _context_score(context_length),
        "recency": _recency_score(created, scoring.recency_half_life_days, current_time),
        "model_tier": _model_tier_score(model_id),
        "stability": _stability_score(model_id),
    }
    weights = {
        "capabilities": scoring.capabilities_weight,
        "context": scoring.context_weight,
        "recency": scoring.recency_weight,
        "model_tier": scoring.model_tier_weight,
        "stability": scoring.stability_weight,
    }
    weight_total = sum(weights.values())
    if weight_total <= 0:
        raise ValueError("At least one scoring weight must be greater than zero")

    weighted_components = {
        name: raw_components[name] * weight / weight_total
        for name, weight in weights.items()
    }
    score = sum(weighted_components.values())
    model["_capability_score"] = raw_components["capabilities"]
    model["_score_components"] = {
        **{name: round(value, 6) for name, value in raw_components.items()},
        "provider_adjustment": 0.0,
    }
    return score


def filter_free_models(models: list) -> list:
    """Filter models to those advertised as free."""
    free_models = []
    for model in models:
        model_id = model.get("id", "")
        pricing = model.get("pricing")
        gateway_provider = model.get("_gateway_provider", "")
        is_openrouter = (
            "openrouter" in gateway_provider.lower()
            or "openrouter" in model.get("owned_by", "").lower()
        )
        if not is_openrouter:
            free_models.append(model)
            continue
        if pricing is not None:
            prompt_cost = pricing.get("prompt")
            try:
                if prompt_cost is not None and float(prompt_cost) == 0:
                    free_models.append(model)
                    continue
            except (ValueError, TypeError):
                pass
        if ":free" in model_id and model not in free_models:
            free_models.append(model)
    return free_models


def filter_paid_models(models: list) -> list:
    """Filter models not classified as free."""
    free_ids = {str(m.get("id")) for m in filter_free_models(models)}
    return [m for m in models if str(m.get("id")) not in free_ids]


def rank_models(models: list, only_free: bool = False) -> list:
    """Rank models and apply a tightly bounded provider adjustment."""
    try:
        if not models:
            return []
        filtered = filter_free_models(models) if only_free else models
        scored_models = []
        from app.config import config

        adjustment_limit = config.settings.scoring.max_provider_adjustment
        for model in filtered:
            if not isinstance(model, dict):
                continue
            try:
                score = calculate_model_score(model)
                provider_name = model.get("_gateway_provider")
                adjustment = 0.0
                if provider_name and provider_name in config.providers:
                    configured = config.providers[provider_name].score_offset
                    adjustment = max(-adjustment_limit, min(configured, adjustment_limit))
                score = max(0.0, min(1.0, score + adjustment))
                model["_score_components"]["provider_adjustment"] = round(adjustment, 6)
                model["_score"] = score
                scored_models.append(model)
            except Exception as exc:
                logger.warning(f"Error scoring individual model: {exc}")
                model["_score"] = 0.0
                scored_models.append(model)

        return sorted(
            scored_models,
            key=lambda item: (-item.get("_score", 0), str(item.get("id", ""))),
        )
    except Exception as exc:
        logger.error(f"Critical error in rank_models: {exc}")
        return models
