"""Performance tracing: transactions, spans and W3C trace context.

Traces propagate with the W3C ``traceparent`` header, so they connect with any
OpenTelemetry-instrumented service as well as other ErrorLens SDKs.
"""

from __future__ import annotations

import random
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from . import scope as _scope

if TYPE_CHECKING:
    from .client import Client

MAX_SPANS = 1000
_TRACEPARENT = re.compile(r"^\s*([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})\s*$")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def new_span_id() -> str:
    return uuid.uuid4().hex[16:]


def parse_traceparent(value: Optional[str]) -> Optional[Tuple[str, str, bool]]:
    """(trace_id, parent_span_id, sampled) from a traceparent header, if valid."""
    if not value:
        return None
    m = _TRACEPARENT.match(value.lower())
    if not m or m.group(1) == "ff" or set(m.group(2)) == {"0"} or set(m.group(3)) == {"0"}:
        return None
    return m.group(2), m.group(3), bool(int(m.group(4), 16) & 1)


def http_status_to_span_status(code: Optional[int]) -> str:
    if code is None:
        return "unknown"
    if code < 400:
        return "ok"
    return {400: "invalid_argument", 401: "unauthenticated", 403: "permission_denied", 404: "not_found",
            409: "already_exists", 429: "resource_exhausted", 499: "cancelled", 501: "unimplemented",
            503: "unavailable", 504: "deadline_exceeded"}.get(code, "internal_error" if code >= 500 else "invalid_argument")


class Span:
    """A timed operation. Use as a context manager, or call finish()."""

    def __init__(self, op: str, description: Optional[str], trace_id: str, parent_span_id: Optional[str],
                 transaction: Optional["Transaction"], sampled: bool):
        self.op = op
        self.description = description
        self.trace_id = trace_id
        self.span_id = new_span_id()
        self.parent_span_id = parent_span_id
        self.transaction = transaction
        self.sampled = sampled
        self.status: Optional[str] = None
        self.data: Dict[str, Any] = {}
        self.start_timestamp = time.time()
        self._start_mono = time.perf_counter()
        self.timestamp: Optional[float] = None
        self._token: Any = None
        self._previous: Optional[Span] = None

    # --- context manager ------------------------------------------------------

    def __enter__(self) -> "Span":
        current = _scope.get_current_scope()
        self._previous = current.span
        current.span = self
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None and self.status is None:
            self.status = "internal_error"
        self.finish()
        _scope.get_current_scope().span = self._previous

    # --- API ------------------------------------------------------------------

    def set_data(self, key: str, value: Any) -> None:
        self.data[key] = value

    def set_status(self, status: str) -> None:
        self.status = status

    def set_http_status(self, code: int) -> None:
        self.status = http_status_to_span_status(code)
        self.data["http.response.status_code"] = code

    def start_child(self, op: str, description: Optional[str] = None) -> "Span":
        return Span(op, description, self.trace_id, self.span_id, self.transaction, self.sampled)

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{'01' if self.sampled else '00'}"

    def finish(self) -> None:
        if self.timestamp is not None:
            return
        self.timestamp = self.start_timestamp + (time.perf_counter() - self._start_mono)
        if self.transaction is not None and self.transaction is not self:
            self.transaction._finished_child(self)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "trace_id": self.trace_id, "span_id": self.span_id, "parent_span_id": self.parent_span_id,
            "op": self.op, "description": self.description, "start_timestamp": self.start_timestamp,
            "timestamp": self.timestamp or time.time(), "status": self.status or "ok",
        }
        if self.data:
            d["data"] = self.data
        return d


class Transaction(Span):
    """The root span of a request or task; sent as one event when finished."""

    def __init__(self, name: str, op: str, client: Optional["Client"], trace_id: Optional[str] = None,
                 parent_span_id: Optional[str] = None, sampled: Optional[bool] = None,
                 context: Optional[Dict[str, Any]] = None):
        trace_id = trace_id or new_trace_id()
        if sampled is None and client is not None:
            sampled = client.sample_transaction(name, op, parent_sampled=None, context=context or {})
        super().__init__(op, None, trace_id, parent_span_id, None, bool(sampled))
        self.transaction = self
        self.name = name
        self.client = client
        self.tags: Dict[str, str] = {}
        self._spans: List[Span] = []
        self._dropped_spans = 0

    def set_name(self, name: str) -> None:
        self.name = name

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = str(value)

    def _finished_child(self, span: Span) -> None:
        if not self.sampled:
            return
        if len(self._spans) < MAX_SPANS:
            self._spans.append(span)
        else:
            self._dropped_spans += 1

    def finish(self) -> None:
        if self.timestamp is not None:
            return
        super().finish()
        if self.sampled and self.client is not None:
            self.client.capture_transaction(self)

    def to_event(self) -> Dict[str, Any]:
        trace: Dict[str, Any] = {"trace_id": self.trace_id, "span_id": self.span_id, "op": self.op,
                                 "status": self.status or "ok"}
        if self.parent_span_id:
            trace["parent_span_id"] = self.parent_span_id
        if self.data:
            trace["data"] = self.data
        event: Dict[str, Any] = {
            "type": "transaction",
            "event_id": uuid.uuid4().hex,
            "transaction": self.name,
            "start_timestamp": self.start_timestamp,
            "timestamp": self.timestamp or time.time(),
            "contexts": {"trace": trace},
            "spans": [s.to_dict() for s in self._spans],
        }
        if self.tags:
            event["tags"] = dict(self.tags)
        if self._dropped_spans:
            event.setdefault("extra", {})["dropped_spans"] = self._dropped_spans
        return event


class _NoOpSpan(Span):
    """Returned when there is nothing to attach to; costs nothing."""

    def __init__(self) -> None:  # noqa: D401
        super().__init__("noop", None, "0" * 32, None, None, False)

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        pass

    def finish(self) -> None:
        pass


def current_span() -> Optional[Span]:
    return _scope.get_current_scope().span


def start_span(op: str, description: Optional[str] = None) -> Span:
    """A child of the active span; a no-op when no sampled transaction is running."""
    parent = current_span()
    if parent is None or not parent.sampled:
        return _NoOpSpan()
    return parent.start_child(op, description)


def continue_trace(headers: Mapping[str, str]) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    """(trace_id, parent_span_id, sampled) from incoming request headers."""
    value = None
    for k, v in headers.items():
        if k.lower() == "traceparent":
            value = v
            break
    parsed = parse_traceparent(value)
    if parsed is None:
        return None, None, None
    return parsed


def outgoing_traceparent() -> Optional[str]:
    """The traceparent header to add to outgoing requests."""
    span = current_span()
    if span is not None and not isinstance(span, _NoOpSpan):
        return span.to_traceparent()
    iso = _scope.get_isolation_scope()
    if iso.trace_id:
        return f"00-{iso.trace_id}-{new_span_id()}-00"
    return None


def trace_context_for_event() -> Dict[str, Any]:
    """The trace an error or log belongs to: the active span, else the request's trace."""
    span = current_span()
    if span is not None and not isinstance(span, _NoOpSpan):
        return {"trace_id": span.trace_id, "span_id": span.span_id, "parent_span_id": span.parent_span_id,
                "op": span.op}
    iso = _scope.get_isolation_scope()
    if not iso.trace_id:
        iso.trace_id = new_trace_id()
    ctx: Dict[str, Any] = {"trace_id": iso.trace_id, "span_id": new_span_id()}
    if iso.parent_span_id:
        ctx["parent_span_id"] = iso.parent_span_id
    return ctx


def sample(rate: Optional[float]) -> bool:
    return bool(rate) and random.random() < float(rate)  # type: ignore[arg-type]
