import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import config
from app.routers import chat, stats
from app.services.router import _providers, get_provider_instance
from app.scoring import rank_models

# ----------------------------
# LOGGING UNIFICATION
# ----------------------------
fl_logger = logging.getLogger("freelance")
fl_logger.setLevel(logging.INFO)
fl_logger.propagate = False  # Avoid duplicates from root

# Clear ALL existing handlers to enable idempotent configuration
for h in fl_logger.handlers[:]:
    fl_logger.removeHandler(h)
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

# 1. Define Formatter
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(message)s",
    "%Y-%m-%d %H:%M:%S"
)

# 2. Add Handlers based on environment
LOG_FILE = "freelance.log"

if os.environ.get("FREELANCE_DAEMON") == "1":
    # DAEMON MODE: OS handles redirection to file via stdout.
    # We only need to provide the stream to stdout.
    cons_h = logging.StreamHandler()
    cons_h.setFormatter(formatter)
    fl_logger.addHandler(cons_h)
else:
    # NORMAL MODE: We handle both terminal output AND manual file backup.
    # Clear and set File Handler
    file_h = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_h.setFormatter(formatter)
    fl_logger.addHandler(file_h)
    
    # Add Console Handler
    cons_h = logging.StreamHandler()
    cons_h.setFormatter(formatter)
    fl_logger.addHandler(cons_h)

logger = fl_logger
# ----------------------------

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
        for res in results:
            all_models.extend(res)

        if all_models:
            # Rank models based on preferences
            ranked = rank_models(all_models, only_free=False)
            config.auto_models_cache = ranked
            config.auto_models_last_update = time.time()
            logger.info(f"✅ [CACHE] Auto-routing cache updated with {len(ranked)} models")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("=" * 60)
    logger.info("  Freelance API Gateway starting up")
    logger.info(f"  Providers: {list(config.providers.keys())}")
    logger.info(f"  Model routes: {len(config.model_routes)}")
    logger.info(f"  Default provider: {config.default_provider}")
    logger.info("=" * 60)
    
    # 1. Initialize providers eagerly
    for name, p_config in config.providers.items():
        # Always try to initialize local or defined providers. 
        # Some (like Ollama) don't require keys.
        get_provider_instance(name, force_create=True)
    
    # 2. Initial Hot Load: Ensure cache is ready BEFORE yielding
    logger.info("⚡ [STARTUP] Performing initial model cache hot load...")
    await update_models_cache()
    logger.info("🚀 [STARTUP] Initial hot load complete.")
    
    # 3. Start background task
    task = asyncio.create_task(background_cache_updater())
    
    yield
    task.cancel()
    logger.info("Freelance API Gateway shutting down")


app = FastAPI(
    title="Freelance API Gateway",
    description="Unified OpenAI-compatible API gateway for multiple LLM providers",
    version="1.0.0",
    lifespan=lifespan,
)

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
        # Log the unauthorized attempt (with masked token)
        masked = f"{token[:4]}****" if len(token) > 4 else "****"
        logger.warning(
            f"Unauthorized access attempt to {request.url.path} from {request.client.host}. "
            f"Token: {masked}"
        )
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


# Request logging middleware
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Log request details."""
    start = time.time()
    response = await call_next(request)
    elapsed = (time.time() - start) * 1000
    logger.info(
        f"{request.method} {request.url.path} - {response.status_code} - {elapsed:.1f}ms"
    )
    return response


# Dual routing support: Root path and /v1 for full OpenAI-compatibility
app.include_router(chat.router, prefix="/v1")
app.include_router(chat.router)
app.include_router(stats.router)


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
            "models": "/models",
            "health": "/health",
            "stats": "/stats",
        },
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": int(time.time())}
