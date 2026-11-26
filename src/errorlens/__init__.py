"""ErrorLens SDK for Python.

    import errorlens

    errorlens.init(dsn="https://<key>@errorlens.io/<project-id>", release="app@1.0.0")

Unhandled exceptions are reported automatically; see https://errorlens.io/docs/python/.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import time
import warnings
from dataclasses import fields
from typing import Any, Callable, Dict, Optional, TypeVar, Union

from . import scope as _scope
from ._version import __version__
from .client import Client, Options
from .scope import Scope, get_current_scope, get_global_scope, get_isolation_scope, isolation_scope, new_scope
from .tracing import Span, Transaction, current_span, outgoing_traceparent, start_span

__all__ = [
    "__version__", "init", "is_enabled", "get_client", "flush", "close",
    "capture_exception", "capture_message", "capture_event", "last_event_id",
    "add_breadcrumb", "set_user", "set_tag", "set_tags", "set_extra", "set_context", "set_level",
    "new_scope", "isolation_scope", "get_current_scope", "get_isolation_scope", "get_global_scope", "Scope",
    "start_transaction", "start_span", "current_span", "get_traceparent", "Span", "Transaction",
    "monitor", "capture_checkin", "logger",
]

_log = logging.getLogger("errorlens")
_client: Optional[Client] = None
_OPTION_NAMES = {f.name for f in fields(Options)}
F = TypeVar("F", bound=Callable[..., Any])


# --- setup -------------------------------------------------------------------------------------


def init(dsn: Optional[str] = None, **options: Any) -> Client:
    """Start reporting to ErrorLens.

    dsn, release and environment default to the ERRORLENS_DSN, ERRORLENS_RELEASE and
    ERRORLENS_ENVIRONMENT environment variables. Without a DSN the SDK does nothing,
    so it's safe to call in tests and local development.
    """
    global _client
    if _client is not None:
        _client.close(timeout=1.0)
    unknown = sorted(set(options) - _OPTION_NAMES)
    if unknown:
        warnings.warn(f"errorlens.init: unknown option(s) ignored: {', '.join(unknown)}", stacklevel=2)
        for k in unknown:
            options.pop(k)
    opts = Options(dsn=dsn or os.environ.get("ERRORLENS_DSN") or None, **options)
    if opts.debug:
        logging.getLogger("errorlens").setLevel(logging.DEBUG)
    _scope.configure_breadcrumbs(opts.max_breadcrumbs)
    client = Client(opts)
    _client = client
    if client.active:
        from .integrations import setup_integrations

        client.integrations = setup_integrations(client)
    elif opts.dsn is None and opts.debug:
        _log.debug("errorlens: no DSN set; reporting is off")
    return client


def get_client() -> Optional[Client]:
    return _client


def is_enabled() -> bool:
    return bool(_client and _client.active)


def flush(timeout: Optional[float] = None) -> bool:
    """Wait until queued events are sent. Call before short scripts exit."""
    return _client.flush(timeout) if _client else True


def close(timeout: Optional[float] = None) -> None:
    global _client
    if _client:
        _client.close(timeout)
    _client = None


# --- capturing ---------------------------------------------------------------------------------


def capture_exception(error: Optional[BaseException] = None, level: str = "error") -> Optional[str]:
    """Report an exception (the one being handled if omitted). Returns the event id."""
    return _client.capture_exception(error, level=level) if _client else None


def capture_message(message: str, level: str = "info") -> Optional[str]:
    return _client.capture_message(message, level) if _client else None


def capture_event(event: Dict[str, Any], hint: Optional[dict] = None) -> Optional[str]:
    return _client.capture_event(dict(event), hint) if _client else None


def last_event_id() -> Optional[str]:
    return _client.last_event_id if _client else None


def add_breadcrumb(crumb: Optional[Dict[str, Any]] = None, hint: Optional[dict] = None, **kwargs: Any) -> None:
    """add_breadcrumb(category="auth", message="User signed in", level="info", data={...})"""
    if _client:
        _client.add_breadcrumb(dict(crumb or {}, **kwargs), hint)


# --- context (applies to the current request or task) --------------------------------------------


def set_user(user: Optional[Dict[str, Any]]) -> None:
    get_isolation_scope().set_user(user)


def set_tag(key: str, value: Any) -> None:
    get_isolation_scope().set_tag(key, value)


def set_tags(tags: Dict[str, Any]) -> None:
    get_isolation_scope().set_tags(tags)


def set_extra(key: str, value: Any) -> None:
    get_isolation_scope().set_extra(key, value)


def set_context(key: str, value: Optional[Dict[str, Any]]) -> None:
    get_isolation_scope().set_context(key, value)


def set_level(level: str) -> None:
    get_isolation_scope().set_level(level)


# --- tracing -----------------------------------------------------------------------------------


def start_transaction(name: str, op: str = "function", traceparent: Optional[str] = None) -> Transaction:
    """A transaction for work no integration covers, e.g. a script or job:

        with errorlens.start_transaction("nightly-export", op="task"):
            ...
    """
    from .tracing import parse_traceparent

    parent = parse_traceparent(traceparent)
    if _client is None:
        return Transaction(name, op, None, sampled=False)
    if parent:
        return _client.start_transaction(name, op, parent[0], parent[1], parent[2])
    return _client.start_transaction(name, op)


def get_traceparent() -> Optional[str]:
    """The W3C traceparent header value to propagate the current trace."""
    return outgoing_traceparent()


# --- cron monitors -----------------------------------------------------------------------------


def capture_checkin(monitor_slug: str, status: str, check_in_id: Optional[str] = None,
                    duration: Optional[float] = None, monitor_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Report a cron run: status is "in_progress", "ok" or "error"."""
    return _client.capture_checkin(monitor_slug, status, check_in_id, duration, monitor_config) if _client else None


class monitor:  # noqa: N801 - used as a decorator and a context manager
    """Report a scheduled job's runs to a cron monitor.

        @errorlens.monitor("nightly-backup", schedule="0 2 * * *")
        def backup(): ...

        with errorlens.monitor("sync-inventory"):
            sync()

    With a schedule, the monitor is created or updated from code.
    """

    def __init__(self, slug: str, schedule: Optional[str] = None, timezone: str = "UTC",
                 checkin_margin: Optional[int] = None, max_runtime: Optional[int] = None):
        self.slug = slug
        self.config: Optional[Dict[str, Any]] = None
        if schedule:
            self.config = {"schedule": {"type": "crontab", "value": schedule}, "timezone": timezone}
            if checkin_margin is not None:
                self.config["checkin_margin"] = checkin_margin
            if max_runtime is not None:
                self.config["max_runtime"] = max_runtime
        self._id: Optional[str] = None
        self._start = 0.0

    def __enter__(self) -> "monitor":
        self._start = time.monotonic()
        self._id = capture_checkin(self.slug, "in_progress", monitor_config=self.config)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        capture_checkin(self.slug, "error" if exc_type else "ok", self._id,
                        duration=time.monotonic() - self._start, monitor_config=self.config)

    def __call__(self, fn: F) -> F:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                m = monitor(self.slug)
                m.config = self.config
                with m:
                    return await fn(*args, **kwargs)
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            m = monitor(self.slug)
            m.config = self.config
            with m:
                return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]


# --- structured logs ---------------------------------------------------------------------------


class _Logger:
    """Structured logs sent to ErrorLens (needs init(enable_logs=True)).

        errorlens.logger.info("Order {order_id} paid", order_id=42, amount=9.5)

    Keyword arguments fill the template and become searchable attributes.
    Records from Python's standard logging module are sent too.
    """

    def _log(self, level: str, template: str, kwargs: Dict[str, Any]) -> None:
        if not _client:
            return
        try:
            body = template.format(**kwargs) if kwargs else template
        except (KeyError, IndexError, ValueError):
            body = template
        attrs: Dict[str, Union[str, int, float, bool]] = {"message.template": template} if kwargs else {}
        attrs.update(kwargs)
        _client.capture_log(level, body, attrs)

    def trace(self, template: str, **kwargs: Any) -> None:
        self._log("trace", template, kwargs)

    def debug(self, template: str, **kwargs: Any) -> None:
        self._log("debug", template, kwargs)

    def info(self, template: str, **kwargs: Any) -> None:
        self._log("info", template, kwargs)

    def warning(self, template: str, **kwargs: Any) -> None:
        self._log("warn", template, kwargs)

    warn = warning

    def error(self, template: str, **kwargs: Any) -> None:
        self._log("error", template, kwargs)

    def fatal(self, template: str, **kwargs: Any) -> None:
        self._log("fatal", template, kwargs)


logger = _Logger()

