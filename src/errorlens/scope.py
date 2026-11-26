"""Scopes: the context attached to events.

Three layers are merged into every event, most specific last:

* the **global** scope — data for the whole process;
* the **isolation** scope — one per request or task (framework integrations
  fork it), so tags, user and breadcrumbs of one request never leak into
  another, also under asyncio and threads;
* the **current** scope — narrowed for a block of code with ``new_scope()``.

Outside any request, all threads share the root isolation scope, so data set
at startup applies everywhere.
"""

from __future__ import annotations

import contextlib
import copy
from collections import deque
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, Iterator, List, Optional

if TYPE_CHECKING:
    from .tracing import Span

Breadcrumb = Dict[str, Any]
EventProcessor = Callable[[dict, dict], Optional[dict]]


class Scope:
    def __init__(self, max_breadcrumbs: int = 100) -> None:
        self.tags: Dict[str, str] = {}
        self.extras: Dict[str, Any] = {}
        self.contexts: Dict[str, Dict[str, Any]] = {}
        self.user: Optional[Dict[str, Any]] = None
        self.level: Optional[str] = None
        self.fingerprint: Optional[List[str]] = None
        self.transaction_name: Optional[str] = None
        self.request: Optional[Dict[str, Any]] = None
        self.breadcrumbs: Deque[Breadcrumb] = deque(maxlen=max_breadcrumbs)
        self.span: Optional["Span"] = None
        self.processors: List[EventProcessor] = []
        # Trace used for errors outside a transaction, so errors and logs of
        # one request still share a trace id.
        self.trace_id: Optional[str] = None
        self.parent_span_id: Optional[str] = None

    # --- setters -------------------------------------------------------------

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[str(key)[:200]] = str(value)[:200]

    def set_tags(self, tags: Dict[str, Any]) -> None:
        for k, v in tags.items():
            self.set_tag(k, v)

    def remove_tag(self, key: str) -> None:
        self.tags.pop(key, None)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def set_context(self, key: str, value: Optional[Dict[str, Any]]) -> None:
        if value is None:
            self.contexts.pop(key, None)
        else:
            self.contexts[key] = dict(value)

    def set_user(self, user: Optional[Dict[str, Any]]) -> None:
        self.user = dict(user) if user else None

    def set_level(self, level: Optional[str]) -> None:
        self.level = level

    def add_breadcrumb(self, crumb: Breadcrumb) -> None:
        self.breadcrumbs.append(crumb)

    def clear_breadcrumbs(self) -> None:
        self.breadcrumbs.clear()

    def add_event_processor(self, fn: EventProcessor) -> None:
        self.processors.append(fn)

    def clear(self) -> None:
        maxlen = self.breadcrumbs.maxlen or 100
        self.__init__(maxlen)  # type: ignore[misc]

    # --- forking -------------------------------------------------------------

    def fork(self) -> "Scope":
        new = Scope.__new__(Scope)
        new.tags = dict(self.tags)
        new.extras = dict(self.extras)
        new.contexts = copy.deepcopy(self.contexts)
        new.user = dict(self.user) if self.user else None
        new.level = self.level
        new.fingerprint = list(self.fingerprint) if self.fingerprint else None
        new.transaction_name = self.transaction_name
        new.request = self.request
        new.breadcrumbs = deque(self.breadcrumbs, maxlen=self.breadcrumbs.maxlen)
        new.span = self.span
        new.processors = list(self.processors)
        new.trace_id = self.trace_id
        new.parent_span_id = self.parent_span_id
        return new

    # --- applying ------------------------------------------------------------

    def apply(self, event: dict, hint: dict) -> Optional[dict]:
        if self.tags:
            event.setdefault("tags", {}).update({k: v for k, v in self.tags.items() if k not in event["tags"]})
        if self.extras:
            event.setdefault("extra", {}).update({k: v for k, v in self.extras.items() if k not in event["extra"]})
        for key, value in self.contexts.items():
            event.setdefault("contexts", {}).setdefault(key, dict(value))
        if self.user and "user" not in event:
            event["user"] = dict(self.user)
        if self.level and "level" not in event:
            event["level"] = self.level
        if self.fingerprint and "fingerprint" not in event:
            event["fingerprint"] = list(self.fingerprint)
        if self.transaction_name and "transaction" not in event:
            event["transaction"] = self.transaction_name
        if self.request and "request" not in event:
            event["request"] = dict(self.request)
        if self.breadcrumbs and event.get("type") != "transaction":
            existing = event.setdefault("breadcrumbs", {"values": []})
            existing["values"] = list(self.breadcrumbs) + existing.get("values", [])
        for fn in self.processors:
            result = fn(event, hint)
            if result is None:
                return None
            event = result
        return event


# --- the three layers ----------------------------------------------------------------

_global = Scope()
_root_isolation = Scope()
_root_current = Scope()
_isolation: ContextVar[Optional[Scope]] = ContextVar("errorlens_isolation", default=None)
_current: ContextVar[Optional[Scope]] = ContextVar("errorlens_current", default=None)


def get_global_scope() -> Scope:
    return _global


def get_isolation_scope() -> Scope:
    return _isolation.get() or _root_isolation


def get_current_scope() -> Scope:
    return _current.get() or _root_current


def configure_breadcrumbs(max_breadcrumbs: int) -> None:
    for s in (_global, _root_isolation, _root_current):
        s.breadcrumbs = deque(s.breadcrumbs, maxlen=max_breadcrumbs)


@contextlib.contextmanager
def new_scope() -> Iterator[Scope]:
    """A copy of the current scope for the duration of a block."""
    token = _current.set(get_current_scope().fork())
    try:
        yield _current.get()  # type: ignore[misc]
    finally:
        _current.reset(token)


@contextlib.contextmanager
def isolation_scope(fresh_breadcrumbs: bool = False) -> Iterator[Scope]:
    """A fresh isolation scope (and current scope) — one per request or task.

    Framework integrations pass fresh_breadcrumbs=True so a request's errors
    show only what happened during that request.
    """
    forked = get_isolation_scope().fork()
    if fresh_breadcrumbs:
        forked.clear_breadcrumbs()
    iso_token = _isolation.set(forked)
    cur_token = _current.set(get_current_scope().fork())
    try:
        yield _isolation.get()  # type: ignore[misc]
    finally:
        _current.reset(cur_token)
        _isolation.reset(iso_token)


def apply_scopes(event: dict, hint: dict) -> Optional[dict]:
    # Most specific first: setdefault-style merging keeps the first value.
    for scope in (get_current_scope(), get_isolation_scope(), _global):
        result = scope.apply(event, hint)
        if result is None:
            return None
        event = result
    return event


def reset_for_tests() -> None:
    global _global, _root_isolation, _root_current
    _global, _root_isolation, _root_current = Scope(), Scope(), Scope()
    _isolation.set(None)
    _current.set(None)
