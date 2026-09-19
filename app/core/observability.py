"""Request-scoped events without buffering request or response bodies."""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import time
import uuid

request_id = ContextVar("gateway_request_id", default=None)


def log_event(logger, event, level=logging.INFO, **fields):
    logger.log(level, event, extra={"event": event, "fields": fields})


class EventFormatter(logging.Formatter):
    def __init__(self, settings, secrets=lambda: ()):
        super().__init__()
        self.settings = settings
        self.secrets = secrets

    def redact(self, value):
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if k.lower() in {
                "authorization", "cookie", "api_key", "password", "token"
            } else self.redact(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(set(self.secrets()), key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value[:self.settings.max_field_chars]
        return value

    def format(self, record):
        fields = dict(getattr(record, "fields", {}))
        if not self.settings.log_request_payload:
            fields.pop("request_payload", None)
        data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "diagnostic"),
            "request_id": request_id.get(),
            "fields": fields,
        }
        # Free-form upstream errors can echo prompts or credentials. They are opt-in.
        if self.settings.include_diagnostics:
            data["message"] = record.getMessage()
        elif not hasattr(record, "event"):
            data["source"] = f"{record.module}:{record.lineno}"
        if record.exc_info:
            data["exception_type"] = record.exc_info[0].__name__
        data = self.redact(data)
        if self.settings.format == "text":
            return f"{data['timestamp']} | {data['level']} | {data['event']} | " + json.dumps(data, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)


def configure_logging(settings, secrets, daemon=False):
    logger = logging.getLogger("freelance")
    logger.setLevel(settings.level)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    formatter = EventFormatter(settings, secrets)
    handlers = [logging.StreamHandler()]
    if not daemon:
        handlers.append(logging.FileHandler("freelance.log", mode="a", encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


class RequestEventMiddleware:
    """Pure ASGI middleware keeps context alive until streaming actually finishes."""

    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger("freelance.http")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = request_id.set(uuid.uuid4().hex)
        start = time.perf_counter()
        status = None
        byte_count = 0
        completed = False
        disconnected = False
        outcome = "incomplete"
        log_event(self.logger, "request_started", method=scope["method"], path=scope["path"])

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal status, byte_count, completed
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                log_event(self.logger, "response_started", status_code=status,
                          elapsed_ms=round((time.perf_counter() - start) * 1000, 3))
            elif message["type"] == "http.response.body":
                byte_count += len(message.get("body", b""))
                if not message.get("more_body", False):
                    completed = True

        try:
            await self.app(scope, observed_receive, observed_send)
            outcome = "completed" if completed else "incomplete"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "failed"
            log_event(self.logger, "request_failed", logging.ERROR, error_type=type(exc).__name__)
            raise
        finally:
            log_event(self.logger, "request_finished", status_code=status,
                      outcome="disconnected" if disconnected and not completed else outcome,
                      bytes_sent=byte_count,
                      elapsed_ms=round((time.perf_counter() - start) * 1000, 3))
            request_id.reset(token)
