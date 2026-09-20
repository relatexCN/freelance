"""Bounded response metadata inspection; never retains or logs conversation text."""
import json


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
                    stream_error=self.error, done_received=self.done,
                    inspection_incomplete=self.incomplete)
