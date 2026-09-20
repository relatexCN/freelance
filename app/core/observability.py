"""Request-scoped events without buffering request or response bodies."""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import hashlib
import hmac
import secrets
import time
import uuid

request_id = ContextVar("gateway_request_id", default=None)
_fingerprint_key = secrets.token_bytes(32)


def request_fingerprint(request):
    """Process-local HMAC: equality hint, not an idempotency or session identifier."""
    body = json.dumps(request.model_dump(mode="json", exclude_none=True),
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(_fingerprint_key, body.encode("utf-8"), hashlib.sha256).hexdigest()


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
                "authorization", "cookie", "api_key", "password", "token",
                "access_token", "refresh_token", "secret", "client_secret", "appsecret"
            } else self.redact(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            # Tool arguments are often JSON encoded inside a string.
            if value.lstrip().startswith(("{", "[")):
                try:
                    value = json.dumps(self.redact(json.loads(value)), ensure_ascii=False)
                except (ValueError, RecursionError):
                    pass
            for secret in sorted(set(self.secrets()), key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
            value = re.sub(r"(?i)((?:access_token|api_key|appsecret|client_secret)=)[^&\s\"']+",
                           r"\1[REDACTED]", value)
            limit = self.settings.max_field_chars
            return value if len(value) <= limit else value[:limit] + "...[TRUNCATED]"
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
        if data["event"] in {"request_payload", "response_payload"}:
            if len(json.dumps(data["fields"], ensure_ascii=False).encode("utf-8")) > self.settings.max_body_bytes:
                data["fields"] = {"omitted": True, "reason": "content_size_limit"}
        if self.settings.format == "text":
            return f"{data['timestamp']} | {data['level']} | {data['event']} | " + json.dumps(data, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)


class EventFilter(logging.Filter):
    def __init__(self, settings, content=False):
        super().__init__()
        self.settings, self.content = settings, content

    def filter(self, record):
        event = getattr(record, "event", None)
        is_content = event in {"request_payload", "response_payload"}
        if self.content:
            return ((event == "request_payload" and self.settings.log_request_payload)
                    or (event == "response_payload" and self.settings.log_response_payload))
        return not is_content and (event is not None or self.settings.include_diagnostics
                                   or record.levelno >= logging.WARNING)


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
        handlers.append(RotatingFileHandler("freelance.log", maxBytes=settings.max_log_bytes,
                        backupCount=settings.backup_count, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(EventFilter(settings))
        logger.addHandler(handler)
    if settings.log_request_payload or settings.log_response_payload:
        handler = RotatingFileHandler(settings.content_log_path, maxBytes=settings.max_log_bytes,
                                      backupCount=settings.backup_count, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.addFilter(EventFilter(settings, content=True))
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
        from app.config import config
        settings = config.settings.logging
        capture = settings.log_response_payload and scope["path"].rstrip("/").endswith(
            ("/chat/completions", "/chat/completions/multi"))
        body = bytearray()
        content_type = ""
        outcome = "incomplete"
        log_event(self.logger, "request_started", method=scope["method"], path=scope["path"])

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal status, byte_count, completed, content_type
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                content_type = next((v.decode("latin1") for k, v in headers if k.lower() == b"content-type"), "")
                message = {**message, "headers": headers + [(b"x-request-id", request_id.get().encode())]}
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
                log_event(self.logger, "response_started", status_code=status,
                          elapsed_ms=round((time.perf_counter() - start) * 1000, 3))
            elif message["type"] == "http.response.body":
                byte_count += len(message.get("body", b""))
                if capture:
                    remaining = max(0, settings.max_body_bytes - len(body))
                    body.extend(message.get("body", b"")[:remaining])
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
            if capture:
                # Never log unparseable raw bytes: they may contain unredacted credentials.
                payload = None
                omitted = byte_count > settings.max_body_bytes or not completed
                if not omitted:
                    try:
                        if "text/event-stream" in content_type:
                            payload = [json.loads(line[5:].strip()) for line in body.decode().splitlines()
                                       if line.startswith("data:") and line[5:].strip() != "[DONE]"]
                        else:
                            payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        omitted = True
                log_event(self.logger, "response_payload", response_payload=payload,
                          omitted=omitted, bytes_sent=byte_count)
            log_event(self.logger, "request_finished", status_code=status,
                      outcome="disconnected" if disconnected and not completed else outcome,
                      bytes_sent=byte_count,
                      elapsed_ms=round((time.perf_counter() - start) * 1000, 3))
            request_id.reset(token)
