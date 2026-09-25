import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import config
from app.routers import chat, stats
from app.services.router import RouterService, _providers, get_provider_instance
from app.scoring import rank_models

from app.core.observability import configure_logging, log_event, RequestEventMiddleware


def _logging_secrets():
    keys = [config.settings.api_key]
    keys.extend(k.strip() for k in config.settings.api_keys.split(","))
    for provider in config.providers.values():
        keys.extend(provider.api_keys)
    return [key for key in keys if key]


logger = configure_logging(config.settings.logging, _logging_secrets,
                           daemon=os.environ.get("FREELANCE_DAEMON") == "1")

LATEST_MODELS_LOG = "latest_models.log"


def _write_latest_models_log(ranked_models: list, providers_with_models: set):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"=== Latest Models ({ts}) ===\n"]

    configured_providers = {name: p_cfg for name, p_cfg in config.providers.items() if p_cfg.api_keys}

    lines.append("--- A: Theoretical Ranking (by score, grouped by provider) ---\n")
    provider_models: dict[str, list] = {}
    for m in ranked_models:
        p_name = m.get("_gateway_provider", "unknown")
        if p_name not in provider_models:
            provider_models[p_name] = []
        provider_models[p_name].append(m)

    provider_order = [p for p in configured_providers if p in provider_models] + \
                     [p for p in provider_models if p not in configured_providers]
    global_idx = 0
    for p_name in provider_order:
        models = provider_models[p_name]
        has_keys = p_name in configured_providers
        key_info = f"[{len(configured_providers[p_name].api_keys)} keys]" if has_keys else "[no keys]"
        lines.append(f"\n  >>> {p_name} {key_info} ({len(models)} models) >>>\n")
        for m in models[:20]:
            global_idx += 1
            model_id = m.get("id", "unknown")
            score = m.get("_score", 0)
            components = m.get("_score_components", {})
            component_note = (
                f" cap={components.get('capabilities', 0):.2f}"
                f" ctx={components.get('context', 0):.2f}"
                f" recent={components.get('recency', 0):.2f}"
                f" tier={components.get('model_tier', 0):.2f}"
                f" stable={components.get('stability', 0):.2f}"
                f" provider_adj={components.get('provider_adjustment', 0):+.3f}"
            )
            lines.append(
                f"    {global_idx:4d}. {model_id:54s}  score={score:.4f}{component_note}\n"
            )
        if len(models) > 20:
            lines.append(f"    ... ({len(models) - 20} more models omitted)\n")

    missing_providers = [p for p in configured_providers if p not in provider_models]
    if missing_providers:
        lines.append(f"\n  --- Providers with no models in cache ---\n")
        for p_name in missing_providers:
            p_cfg = configured_providers[p_name]
            lines.append(f"    {p_name} [{len(p_cfg.api_keys)} keys]: list_models returned empty or failed\n")

    routing_mode = "custom ranking + default fallbacks" if config.custom_model_ranking else "default ranking"
    lines.append(f"\n--- B: Actual Routing ({routing_mode}) ---\n")
    custom_pairs = set(config.custom_model_ranking)
    for actual_idx, (p_name, model_id) in enumerate(RouterService.build_auto_candidates(), 1):
        marker = " [custom]" if (p_name, model_id) in custom_pairs else ""
        in_cache = any(
            m.get("_gateway_provider") == p_name and m.get("id") == model_id
            for m in ranked_models
        )
        cache_note = "" if in_cache else " (not in cache)"
        lines.append(
            f"    {actual_idx:4d}. [{p_name:16s}] {model_id}{marker}{cache_note}\n"
        )

    try:
        with open(LATEST_MODELS_LOG, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logger.error(f"Failed to write latest_models.log: {e}")


async def update_models_cache():
    """Fetch and score models from all providers in parallel."""
    try:
        logger.info("🔄 [CACHE] Updating semantic auto-routing models cache...")
        
        # Get all initialized providers
        active_providers = list(_providers.items())
        if not active_providers:
            return

        # Define the fetch worker
        async def fetch_provider_models(name, provider):
            try:
                p_models = await provider.list_models()
                # Tag them
                for m in p_models:
                    m["_gateway_provider"] = name
                return p_models
            except Exception as e:
                logger.warning(f"Failed to fetch models from provider {name}: {e}")
                return []

        # Execute parallel fetching
        results = await asyncio.gather(
            *[fetch_provider_models(name, provider) for name, provider in active_providers]
        )
        
        all_models = []
        # Track which providers failed to return models
        providers_with_models = set()
        for res in results:
            if res:
                p_name = res[0].get("_gateway_provider", "unknown")
                providers_with_models.add(p_name)
            all_models.extend(res)

        if all_models:
            # Rank models based on preferences
            ranked = rank_models(all_models, only_free=False)
            config.auto_models_cache = ranked
            config.auto_models_last_update = time.time()
            # Store which providers have models in cache (for fallback logic)
            config.providers_with_models_in_cache = providers_with_models
            logger.info(f"✅ [CACHE] Auto-routing cache updated with {len(ranked)} models from {len(providers_with_models)} providers")
            
            _write_latest_models_log(ranked, providers_with_models)
        else:
            logger.warning("⚠️ [CACHE] No models found or ranked. Cache is empty.")
            
    except Exception as e:
        logger.exception(f"❌ [CACHE] Error during models cache update: {e}")


async def background_cache_updater():
    """Periodically fetches and scores models from all providers."""
    while True:
        # Wait for the next update since lifespan handles the initial one
        await asyncio.sleep(6 * 3600)
        await update_models_cache()


async def initialize_service(app: FastAPI):
    """Run slow initialization after HTTP serving can begin."""
    try:
        for name, p_config in config.providers.items():
            if p_config.api_keys:
                get_provider_instance(name, force_create=True)
        logger.info("[STARTUP] Performing initial model cache hot load...")
        await update_models_cache()
        # Discovery is best effort: configured defaults work without a cache.
        app.state.startup_status = "ready"
        log_event(logger, "service_ready")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        app.state.startup_status = "failed"
        log_event(logger, "service_startup_failed", logging.ERROR,
                  exception_type=type(exc).__name__)
        return
    await background_cache_updater()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    app.state.startup_status = "starting"
    log_event(logger, "reliability_settings_loaded",
              connect_timeout_seconds=config.settings.reliability.connect_timeout_seconds,
              first_output_timeout_seconds=config.settings.reliability.first_output_timeout_seconds,
              request_timeout_seconds=config.settings.reliability.request_timeout_seconds,
              model_cooldown_seconds=config.settings.reliability.model_cooldown_seconds,
              model_cooldown_max_seconds=config.settings.reliability.model_cooldown_max_seconds)
    # 0. Gateway Key Validation
    has_gw_key = bool(config.settings.api_key or (config.settings.api_keys and config.settings.api_keys.strip()))
    if not has_gw_key:
        logger.warning("⚠️  [SECURITY] No GATEWAY API Key (API_KEY/API_KEYS) found. Anyone can use this gateway!")
    elif config.settings.api_key and not config.settings.api_key.startswith("sk-freelance-"):
        logger.warning("⚠️  [SECURITY] Unusual Gateway API Key format. (Expected prefix 'sk-freelance-')")
    else:
        logger.info("✅ [SECURITY] Gateway API Key(s) validated.")

    task = asyncio.create_task(initialize_service(app), name="gateway-initialization")
    try:
        yield
    finally:
        app.state.startup_status = "stopping"
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        logger.info("Freelance API Gateway shutting down")


app = FastAPI(
    title="Freelance API Gateway",
    description="Unified OpenAI-compatible API gateway for multiple LLM providers",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.startup_status = "starting"

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Key authentication middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Validate API key if configured."""
    # Skip auth for health check and docs
    if request.url.path in ("/", "/health", "/docs", "/openapi.json", "/redoc"):
        return await call_next(request)

    # Collect all valid keys from environment
    valid_keys = set()
    if config.settings.api_key:
        valid_keys.add(config.settings.api_key)
    if config.settings.api_keys:
        valid_keys.update([k.strip() for k in config.settings.api_keys.split(",") if k.strip()])

    # If no keys are configured, skip authentication entirely
    if not valid_keys:
        return await call_next(request)

    # Authentication is REQUIRED
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""

    if token not in valid_keys:
        log_event(logger, "authentication_failed", logging.WARNING, status_code=401)
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Invalid or missing API key. Please use 'Authorization: Bearer <GATEWAY_KEY>'",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }
            },
        )

    return await call_next(request)


@app.middleware("http")
async def readiness_middleware(request: Request, call_next):
    """Do not dispatch requests or return misleading 404s while warming up."""
    status = request.app.state.startup_status
    if status != "ready":
        messages = {"starting": "服务启动中，请稍后重试。",
                    "failed": "服务启动失败，请检查网关日志。",
                    "stopping": "服务停止中，请稍后重试。"}
        return JSONResponse(status_code=503, headers={"Retry-After": "5", "Cache-Control": "no-store"},
            content={"error": {"message": messages.get(status, "服务暂不可用。"),
                               "type": "service_unavailable", "code": "service_" + status}})
    return await call_next(request)


# Register last so request context surrounds readiness, auth and streaming.
app.add_middleware(RequestEventMiddleware)


# Dual routing support: Root path and /v1 for full OpenAI-compatibility
app.include_router(chat.router, prefix="/v1")
app.include_router(chat.router)
app.include_router(stats.router)
from app.manager_agent.api import router as manager_router
app.include_router(manager_router)


@app.get("/")
async def root():
    """Root endpoint with gateway info."""
    return {
        "name": "Freelance API Gateway",
        "version": "1.0.0",
        "status": "running",
        "providers": list(config.providers.keys()),
        "endpoints": {
            "chat_completions": "/chat/completions",
            "multi_chat_completions": "/chat/completions/multi",
            "models": "/models",
            "health": "/health",
            "stats": "/stats",
        },
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": int(time.time())}


@app.get("/health/providers")
async def provider_health():
    """Provider health status endpoint."""
    now = time.time()
    providers_status = {}
    for name, p_cfg in config.providers.items():
        is_melted = p_cfg.is_platform_melted()
        remaining = int(p_cfg.melted_until - now) if is_melted else 0
        usable_keys = 0
        for idx in range(len(p_cfg.api_keys)):
            if idx not in p_cfg.fatal_keys:
                if idx not in p_cfg.key_cooldowns or now >= p_cfg.key_cooldowns[idx]:
                    usable_keys += 1
        providers_status[name] = {
            "melted": is_melted,
            "melted_remaining_s": remaining,
            "consecutive_failures": p_cfg.consecutive_failures,
            "max_retries_threshold": config.settings.max_retries,
            "fatal_keys": len(p_cfg.fatal_keys),
            "total_keys": len(p_cfg.api_keys),
            "usable_keys": usable_keys,
            "default_model": p_cfg.default_model or None,
        }
    return {"status": "ok", "timestamp": int(now), "providers": providers_status}
