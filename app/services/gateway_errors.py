"""
Structured upstream error classification and status-code rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union


@dataclass(frozen=True)
class StatusCodeRange:
    start: int
    end: int

    def contains(self, code: int) -> bool:
        return self.start <= code <= self.end


def parse_status_code_ranges(value: Optional[Union[str, list[int], list[str]]]) -> list[StatusCodeRange]:
    """Parse comma-separated HTTP status code ranges such as '429,500-503'."""
    if not value:
        return []

    if isinstance(value, list):
        tokens = [str(v).strip() for v in value if str(v).strip()]
    else:
        tokens = [part.strip() for part in str(value).split(",") if part.strip()]

    ranges: list[StatusCodeRange] = []
    for token in tokens:
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(token)

        if start < 100 or end > 599 or start > end:
            raise ValueError(f"Invalid HTTP status code range: {token}")
        ranges.append(StatusCodeRange(start, end))

    ranges.sort(key=lambda r: (r.start, r.end))
    merged: list[StatusCodeRange] = []
    for item in ranges:
        if not merged or item.start > merged[-1].end + 1:
            merged.append(item)
        else:
            merged[-1] = StatusCodeRange(merged[-1].start, max(merged[-1].end, item.end))
    return merged


def matches_status(code: Optional[int], ranges: list[StatusCodeRange]) -> bool:
    if code is None:
        return False
    return any(r.contains(code) for r in ranges)


@dataclass
class GatewayError(Exception):
    """A normalized upstream error used by provider and router layers."""

    message: str
    error_type: str = "upstream_error"
    status_code: Optional[int] = None
    error_code: str = "upstream_error"
    retryable: bool = True
    skip_retry: bool = False
    key_index: int = -1
    disable_key: bool = False
    key_fatal: bool = False
    disable_model: bool = False
    disable_provider: bool = False
    cooldown: Optional[int] = None
    provider: str = ""
    model: str = ""
    exception_type: str = ""
    timeout_phase: str = ""

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.error_code:
            parts.append(f"code={self.error_code}")
        if self.key_index >= 0:
            parts.append(f"key={self.key_index}")
        return " | ".join(parts)

    @classmethod
    def from_exception(cls, exc: Exception, *, provider: str = "", model: str = "", key_index: int = -1):
        if isinstance(exc, GatewayError):
            return exc
        return cls(
            message=str(exc) or repr(exc),
            error_type=type(exc).__name__,
            error_code="exception",
            key_index=key_index,
            provider=provider,
            model=model,
        )
