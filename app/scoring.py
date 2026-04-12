"""
Model scoring and ranking logic based on FreeRide.
"""

import time
import logging
from typing import Any

logger = logging.getLogger("link.scoring")

# Ranking weights from TrueRide
RANKING_WEIGHTS = {
    "context_length": 0.4,      # Prefer longer context
    "capabilities": 0.3,        # Prefer more capabilities
    "recency": 0.2,             # Prefer newer models
    "provider_trust": 0.1       # Prefer trusted providers
}

# Recognized model families for heuristic guessing (context_length, capability_level, recency)
MODEL_SPECS = {
    "llama-3.3": (128000, 0.9, 1733529600), # Newer llama 3.3
    "llama-3.2": (128000, 0.7, 1727308800),
    "llama-3.1": (128000, 0.8, 1721692800),
    "llama-3": (8192, 0.5, 1713484800),
    "gpt-4o": (128000, 1.0, 1715558400),
    "gpt-4.1": (128000, 1.1, 1730000000),
    "o1": (128000, 1.2, 1725000000),
    "gemini-1.5-pro": (1000000, 0.9, 1715558400),
    "gemini-1.5-flash": (1000000, 0.6, 1715558400),
    "gemini-2.0-flash": (1000000, 0.8, 1734000000),
    "qwen2.5": (128000, 0.8, 1726704000),
    "deepseek-v3": (128000, 1.0, 1735000000),
    "deepseek-r1": (128000, 1.2, 1737000000),
}

TRUSTED_PROVIDERS = [
    "google", "meta-llama", "mistralai", "deepseek",
    "nvidia", "qwen", "microsoft", "allenai", "arcee-ai",
    "groq", "cerebras", "github", "sambanova", "mistral", "google-gemini"
]

def heuristically_infer_specs(model_id: str):
    """Attempt to guess context and capability based on model string keywords."""
    model_id = model_id.lower()
    ctx, cap, rel = 8192, 0.2, 0.0
    
    # Try specs map first
    for key, (m_ctx, m_cap, m_rel) in MODEL_SPECS.items():
        if key in model_id:
            ctx, cap, rel = m_ctx, m_cap, m_rel
            break
            
    # Heuristic adjustments
    if "-70b" in model_id or "-large" in model_id:
        cap = max(cap, 0.8)
    if "-405b" in model_id:
        cap = max(cap, 1.0)
    if "flash" in model_id or "nano" in model_id:
        cap = cap * 0.7 # Penalize complex reasoning slightly for speed-focused models
    if "vision" in model_id:
        cap += 0.1
        
    return ctx, cap, rel

def calculate_model_score(model: dict) -> float:
    """
    Calculate a ranking score for a model based on multiple criteria.
    Supports standard OpenAI formats and knowledge-based fallbacks.
    """
    score = 0.0
    model_id = model.get("id", "").lower()

    # Get inferred base stats
    inf_ctx, inf_cap, inf_rel = heuristically_infer_specs(model_id)

    # 1. Context length score (normalized to 0-1, max 1M tokens)
    context_length = int(model.get("context_length", 0)) or inf_ctx
    model["context_length"] = context_length
    
    context_score = min(context_length / 1_000_000, 1.0)
    score += context_score * RANKING_WEIGHTS["context_length"]

    # 2. Capabilities score
    capabilities = model.get("supported_parameters", [])
    capability_count = len(capabilities) if capabilities else 0
    
    if capability_count:
        capability_score = min(capability_count / 10, 1.0)
    else:
        capability_score = inf_cap
        
    model["_capability_score"] = capability_score
    score += capability_score * RANKING_WEIGHTS["capabilities"]

    # 3. Recency score (based on creation date)
    created = 0
    if "created" in model and isinstance(model["created"], (int, float)):
        created = model["created"]
    elif inf_rel:
        created = inf_rel
    
    if created:
        days_old = (time.time() - created) / 86400
        recency_score = max(0, 1 - (days_old / 365))  # Newer models score higher
        score += recency_score * RANKING_WEIGHTS["recency"]

    # 4. Provider trust score
    provider = model_id.split("/")[0] if "/" in model_id else ""
        
    if provider in TRUSTED_PROVIDERS:
        trust_index = TRUSTED_PROVIDERS.index(provider)
        trust_score = 1 - (trust_index / (len(TRUSTED_PROVIDERS) * 2)) # Trust factor
        score += trust_score * RANKING_WEIGHTS["provider_trust"]

    return score


def filter_free_models(models: list) -> list:
    """Filter models to only include free ones."""
    free_models = []

    for model in models:
        model_id = model.get("id", "")
        pricing = model.get("pricing")
        
        # If the model comes from a generic provider that isn't openrouter,
        # we generally assume it's free/allowed for routing if the user configured it,
        # unless it explicitly has pricing > 0.
        gateway_provider = model.get("_gateway_provider", "")
        is_openrouter = "openrouter" in gateway_provider.lower() or "openrouter" in model.get("owned_by", "").lower()

        if not is_openrouter:
            free_models.append(model)
            continue

        # OpenRouter specific filtering
        if pricing is not None:
            prompt_cost = pricing.get("prompt")
            if prompt_cost is not None:
                try:
                    if float(prompt_cost) == 0:
                        free_models.append(model)
                        continue
                except (ValueError, TypeError):
                    pass

        # Also include models with :free suffix
        if ":free" in model_id and model not in free_models:
            free_models.append(model)

    return free_models


def filter_paid_models(models: list) -> list:
    """Filter only for paid models (those not in free_models)."""
    free_ids = {str(m.get("id")) for m in filter_free_models(models)}
    return [m for m in models if str(m.get("id")) not in free_ids]


def rank_models(models: list, only_free: bool = False) -> list:
    """Rank models based on scores. Can optionally filter only for free models."""
    try:
        if not models:
            return []
            
        if only_free:
            filtered = filter_free_models(models)
        else:
            filtered = models

        scored_models = []
        from app.config import config
        
        for model in filtered:
            if not isinstance(model, dict):
                continue
                
            try:
                score = calculate_model_score(model)
                # Apply provider offsets
                p_name = model.get("_gateway_provider")
                if p_name and p_name in config.providers:
                    p_config = config.providers[p_name]
                    score += p_config.score_offset
                    
                model["_score"] = score
                scored_models.append(model)
            except Exception as e:
                logger.warning(f"Error scoring individual model: {e}")
                model["_score"] = 0.1
                scored_models.append(model)

        return sorted(scored_models, key=lambda x: x.get("_score", 0), reverse=True)
    except Exception as e:
        logger.error(f"Critical error in rank_models: {e}")
        return models  # Return unsorted as fallback
