"""Bounded response metadata inspection; never retains or logs conversation text."""
import json
import time


class ResponseAudit:
    def __init__(self, limit=262144):
        self.limit = limit
        self.buffer = bytearray()
        self.dropping = False
        self.incomplete = False
        self.done = False
        self.error = False
        self.finish_reasons = set()
        self.tool_slots = set()
        self.content_chars = 0
        self.gateway_notice_chars = 0
        self.reasoning_chars = 0
        self.reasoning_chunks = 0
        self.started = time.monotonic()
        self.first_content_ms = None
        self.first_tool_ms = None
        self.first_reasoning_ms = None

    def inspect(self, data):
        if not isinstance(data, dict):
            self.incomplete = True
            return
        self.error |= bool(data.get("error"))
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                self.incomplete = True
                continue
            reason = choice.get("finish_reason")
            if reason:
                self.finish_reasons.add(reason if reason in {
                    "stop", "length", "tool_calls", "function_call", "content_filter"
                } else "other")
            delta = choice.get("delta") or choice.get("message") or {}
            content = delta.get("content")
            if isinstance(content, str):
                self.content_chars += len(content)
                if data.get('_gateway_notice') is True:
                    self.gateway_notice_chars += len(content)
                elif content.strip() and self.first_content_ms is None:
                    self.first_content_ms = round((time.monotonic() - self.started) * 1000, 3)
            # Providers may duplicate reasoning across these fields: count one representation.
            reasoning = delta.get('reasoning') or delta.get('reasoning_content')
            if not isinstance(reasoning, str):
                reasoning = ''.join(item.get('text', '') for item in delta.get('reasoning_details') or []
                                    if isinstance(item, dict) and isinstance(item.get('text'), str))
            if reasoning:
                self.reasoning_chars += len(reasoning)
                self.reasoning_chunks += 1
                if self.first_reasoning_ms is None:
                    self.first_reasoning_ms = round((time.monotonic() - self.started) * 1000, 3)
            if (delta.get('tool_calls') or delta.get('function_call')) and self.first_tool_ms is None:
                self.first_tool_ms = round((time.monotonic() - self.started) * 1000, 3)
            for index, tool in enumerate(delta.get("tool_calls") or []):
                if len(self.tool_slots) < 1024:
                    self.tool_slots.add((str(choice.get("index", 0))[:32],
                                         str(tool.get("index", index))[:32]))

    def feed(self, chunk, streaming):
        if not streaming:
            if len(self.buffer) + len(chunk) <= self.limit:
                self.buffer.extend(chunk)
            else:
                self.incomplete = True
            return
        # Provider emits one JSON data line per event. Bytes can split anywhere.
        for part in chunk.splitlines(keepends=True):
            ended = part.endswith((b'\n', b'\r'))
            if len(self.buffer) + len(part) > self.limit:
                self.dropping = True
                self.incomplete = True
                self.buffer.clear()
            if not self.dropping:
                self.buffer.extend(part)
            if ended:
                if not self.dropping:
                    line = bytes(self.buffer).strip()
                    if line.startswith(b'data:'):
                        payload = line[5:].strip()
                        if payload == b'[DONE]':
                            self.done = True
                        else:
                            try:
                                self.inspect(json.loads(payload))
                            except (ValueError, TypeError, AttributeError):
                                self.incomplete = True
                self.buffer.clear()
                self.dropping = False

    def summary(self, streaming, status, completed):
        if not streaming and not self.incomplete:
            try:
                self.inspect(json.loads(self.buffer))
            except (ValueError, TypeError, AttributeError):
                self.incomplete = True
        if self.error or (status is not None and status >= 400):
            outcome = "error"
        elif self.finish_reasons & {"length", "content_filter"}:
            outcome = "truncated"
        elif not completed or self.incomplete or (streaming and (not self.done or not self.finish_reasons)):
            outcome = "incomplete"
        elif not self.finish_reasons:
            outcome = "unknown"
        else:
            outcome = "completed"
        return dict(model_outcome=outcome, finish_reasons=sorted(self.finish_reasons),
                    tool_call_count=len(self.tool_slots), content_chars=self.content_chars,
                    model_content_chars=self.content_chars - self.gateway_notice_chars,
                    gateway_notice_chars=self.gateway_notice_chars,
                    reasoning_chars=self.reasoning_chars, reasoning_chunks=self.reasoning_chunks,
                    first_content_ms=self.first_content_ms, first_tool_ms=self.first_tool_ms,
                    first_reasoning_ms=self.first_reasoning_ms,
                    stream_error=self.error, done_received=self.done,
                    inspection_incomplete=self.incomplete)
