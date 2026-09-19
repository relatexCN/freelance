"""Read-only, bounded management observations. Never exports keys or prompts."""

from dataclasses import asdict
import time

from app.config import config
from app.services.stats_service import stats_service


def snapshot():
    now = time.time()
    providers = []
    for name, provider in config.providers.items():
        providers.append({
            "provider": name,
            "configured": bool(provider.api_keys),
            "key_count": len(provider.api_keys),
            "available_keys": sum(
                index not in provider.fatal_keys and provider.key_cooldowns.get(index, 0) <= now
                for index in range(len(provider.api_keys))
            ),
            "fatal_keys": len(provider.fatal_keys),
            "cooldown_remaining_seconds": max(0, round(provider.melted_until - now)),
            "consecutive_failures": provider.consecutive_failures,
        })
    # get_stats() also cleans up cooldown state. Copy under its lock instead.
    with stats_service._lock:
        stats = [(name, asdict(value)) for name, value in stats_service._stats.items()]
    metrics = []
    for name, value in sorted(stats, key=lambda row: row[1]["last_request_time"], reverse=True)[:100]:
        value.pop("last_error", None)  # Raw upstream errors can echo user messages.
        value["target"] = name
        successes = value["success_count"]
        value["avg_latency_ms"] = value["total_latency_ms"] / successes if successes else None
        metrics.append(value)
    models = [{"provider": m.get("_gateway_provider"), "model": m.get("id"),
               "score": m.get("_score"), "context_length": m.get("context_length")}
              for m in config.auto_models_cache[:100]]
    return {
        "observed_at": now, "providers": providers, "metrics": metrics, "models": models,
        "cache_updated_at": config.auto_models_last_update,
        "limitations": [
            "Metrics are process-local cumulative counters, not time-window trends.",
            "Streaming success can mean first chunk received, not completed inference.",
            "Management model calls are excluded from gateway metrics.",
            "No quota, price or model-quality measurements are available.",
            "Models and metrics are limited to 100 entries each; no active health probes were run.",
        ],
    }
