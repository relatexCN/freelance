import time
import threading
from typing import Dict, Any, Optional
from dataclasses import dataclass, field, asdict

@dataclass
class ProviderMetrics:
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0
    total_ttft_ms: float = 0
    last_error: Optional[str] = None
    last_request_time: float = 0

class StatsService:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(StatsService, cls).__new__(cls)
                cls._instance._stats = {} # (provider, model) -> ProviderMetrics
        return cls._instance

    def record_request(self, provider: str, model: str, latency_ms: float = 0, ttft_ms: float = 0, error: Optional[str] = None):
        key = f"{provider}/{model}"
        with self._lock:
            if key not in self._stats:
                self._stats[key] = ProviderMetrics()
            
            metrics = self._stats[key]
            metrics.request_count += 1
            metrics.last_request_time = time.time()
            
            if error:
                metrics.error_count += 1
                metrics.last_error = error
            else:
                metrics.success_count += 1
                metrics.total_latency_ms += latency_ms
                if ttft_ms > 0:
                    metrics.total_ttft_ms += ttft_ms

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            results = {}
            for key, m in self._stats.items():
                data = asdict(m)
                # Calculate averages
                if m.success_count > 0:
                    data["avg_latency_ms"] = round(m.total_latency_ms / m.success_count, 2)
                    if m.total_ttft_ms > 0:
                        data["avg_ttft_ms"] = round(m.total_ttft_ms / m.success_count, 2)
                
                # Add current melt status from config if available
                from app.config import config
                provider_name = key.split("/")[0]
                p_cfg = config.providers.get(provider_name)
                if p_cfg:
                    data["is_melted"] = p_cfg.is_platform_melted()
                    data["melted_until"] = round(p_cfg.melted_until - time.time(), 0) if data["is_melted"] else 0
                
                results[key] = data
            return results

stats_service = StatsService()
