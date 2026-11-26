"""The client: options and the path every event takes to the transport."""

from __future__ import annotations

import atexit
import logging
import os
import platform
import random
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import scope as _scope
from ._version import __version__
from .dsn import Dsn, InvalidDsn
from .scrub import Scrubber
from .serialize import to_json
from .stacktrace import FrameRules, current_stack, exceptions_from
from .tracing import Transaction, trace_context_for_event
from .transport import HttpTransport, Transport, json_bytes

log = logging.getLogger("errorlens")

LEVELS = ("debug", "info", "warning", "error", "fatal")
LOG_SEVERITY = {"trace": 1, "debug": 5, "info": 9, "warn": 13, "error": 17, "fatal": 21}

# Release names from common CI and hosting environments, when not set explicitly.
_RELEASE_ENV = ("ERRORLENS_RELEASE", "SOURCE_VERSION", "HEROKU_SLUG_COMMIT", "RENDER_GIT_COMMIT",
                "RAILWAY_GIT_COMMIT_SHA", "VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA", "CI_COMMIT_SHA",
                "CODEBUILD_RESOLVED_SOURCE_VERSION", "K_REVISION")


@dataclass
class Options:
    dsn: Optional[str] = None
    release: Optional[str] = None
    environment: Optional[str] = None
    server_name: Optional[str] = None
    enabled: bool = True
    debug: bool = False
    # Errors
    sample_rate: float = 1.0
    ignore_errors: Sequence[Any] = ()
    attach_stacktrace: bool = False
    include_local_variables: bool = True
    include_source_context: bool = True
    in_app_include: Sequence[str] = ()
    in_app_exclude: Sequence[str] = ()
    project_root: Optional[str] = None
    max_breadcrumbs: int = 100
    # Privacy
    send_default_pii: bool = False
    scrub_fields: Sequence[str] = ()
    safe_fields: Sequence[str] = ()
    # Hooks
    before_send: Optional[Callable[[dict, dict], Optional[dict]]] = None
    before_send_transaction: Optional[Callable[[dict, dict], Optional[dict]]] = None
    before_breadcrumb: Optional[Callable[[dict, dict], Optional[dict]]] = None
    before_send_log: Optional[Callable[[dict], Optional[dict]]] = None
    # Performance
    traces_sample_rate: Optional[float] = None
    traces_sampler: Optional[Callable[[Dict[str, Any]], Any]] = None
    trace_propagation_targets: Optional[Sequence[str]] = None  # None = everywhere
    # Logs
    enable_logs: bool = False
    # Integrations
    default_integrations: bool = True
    integrations: Sequence[Any] = ()
    disabled_integrations: Sequence[str] = ()
    # Transport
    transport: Optional[Transport] = None
    max_queue_size: int = 100
    shutdown_timeout: float = 2.0
    http_proxy: Optional[str] = None
    ca_certs: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class Client:
    def __init__(self, options: Options):
        self.options = options
        self.dsn: Optional[Dsn] = None
        if options.dsn:
            try:
                self.dsn = Dsn.parse(options.dsn)
            except InvalidDsn as e:
                log.warning("errorlens: %s — reporting is disabled", e)
        self.active = bool(options.enabled and self.dsn)
        self.frame_rules = FrameRules(options.in_app_include, options.in_app_exclude, options.project_root)
        self.scrubber = Scrubber(options.scrub_fields, options.safe_fields)
        self.transport: Optional[Transport] = None
        if self.active:
            self.transport = options.transport or HttpTransport(
                self.dsn, queue_size=options.max_queue_size, proxy=options.http_proxy, ca_certs=options.ca_certs)  # type: ignore[arg-type]
        self.integrations: List[str] = []
        self.last_event_id: Optional[str] = None
        self._logs: List[Dict[str, Any]] = []
        self._logs_lock = threading.Lock()
        self._logs_timer: Optional[threading.Timer] = None
        self._static = self._static_context()
        if self.active:
            atexit.register(self._atexit)

    # --- static data ---------------------------------------------------------------

    def _static_context(self) -> Dict[str, Any]:
        o = self.options
        release = o.release or next((os.environ[k] for k in _RELEASE_ENV if os.environ.get(k)), None)
        o.release = release
        o.environment = o.environment or os.environ.get("ERRORLENS_ENVIRONMENT") or "production"
        if o.server_name is None:
            try:
                o.server_name = socket.gethostname()
            except Exception:  # noqa: BLE001
                o.server_name = None
        return {
            "runtime": {"name": platform.python_implementation(), "version": platform.python_version(),
                        "build": sys.version.split("\n")[0]},
            "os": {"name": platform.system(), "version": platform.release()},
        }

    def _defaults(self, event: Dict[str, Any]) -> None:
        o = self.options
        event.setdefault("event_id", uuid.uuid4().hex)
        event.setdefault("timestamp", time.time())
        event.setdefault("platform", "python")
        if o.release:
            event.setdefault("release", o.release)
        event.setdefault("environment", o.environment)
        if o.server_name:
            event.setdefault("server_name", o.server_name)
        event["sdk"] = {"name": "errorlens.python", "version": __version__, "integrations": list(self.integrations),
                        "packages": [{"name": "pypi:errorlens", "version": __version__}]}
        contexts = event.setdefault("contexts", {})
        for k, v in self._static.items():
            contexts.setdefault(k, dict(v))

    # --- sampling --------------------------------------------------------------------

    def sample_transaction(self, name: str, op: str, parent_sampled: Optional[bool], context: Dict[str, Any]) -> bool:
        o = self.options
        if not self.active:
            return False
        if o.traces_sampler is not None:
            try:
                rate = o.traces_sampler({"name": name, "op": op, "parent_sampled": parent_sampled, **context})
            except Exception:  # noqa: BLE001
                log.debug("errorlens: traces_sampler raised", exc_info=True)
                return False
            if isinstance(rate, bool):
                return rate
            return bool(rate) and random.random() < float(rate)
        if parent_sampled is not None:
            return parent_sampled
        rate = o.traces_sample_rate
        return bool(rate) and random.random() < float(rate)  # type: ignore[arg-type]

    # --- errors and messages ---------------------------------------------------------

    def capture_exception(self, error: Optional[BaseException] = None, hint: Optional[dict] = None,
                          mechanism: Optional[dict] = None, level: str = "error") -> Optional[str]:
        if error is None:
            error = sys.exc_info()[1]
        if error is None:
            return None
        # One report per exception object: a framework hook, logger.exception() and the thread or
        # exit hook can all see the same exception.
        if getattr(error, "__errorlens_captured__", False):
            return None
        if self._ignored(error):
            return None
        try:
            error.__errorlens_captured__ = True  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - some exception types refuse new attributes
            pass
        o = self.options
        event = {
            "level": level,
            "exception": {"values": exceptions_from(error, self.frame_rules, o.include_local_variables,
                                                    o.include_source_context, mechanism)},
        }
        return self.capture_event(event, dict(hint or {}, exc_info=(type(error), error, error.__traceback__)))

    def capture_message(self, message: str, level: str = "info", hint: Optional[dict] = None) -> Optional[str]:
        event: Dict[str, Any] = {"message": message, "level": level if level in LEVELS else "info"}
        if self.options.attach_stacktrace:
            event["threads"] = {"values": [{"stacktrace": {"frames": current_stack(
                self.frame_rules, self.options.include_source_context, skip=1)}, "current": True}]}
        return self.capture_event(event, hint or {})

    def capture_event(self, event: Dict[str, Any], hint: Optional[dict] = None) -> Optional[str]:
        if not self.active:
            return None
        hint = hint or {}
        try:
            prepared = self._prepare(event, hint)
        except Exception:  # noqa: BLE001 - reporting must never break the app
            log.debug("errorlens: could not prepare event", exc_info=True)
            return None
        if prepared is None:
            return None
        payload = json_bytes(prepared)
        if self.transport and self.transport.submit([({"type": "event"}, payload)], prepared["event_id"]):
            self.last_event_id = prepared["event_id"]
            return prepared["event_id"]
        return None

    def _prepare(self, event: Dict[str, Any], hint: dict) -> Optional[Dict[str, Any]]:
        o = self.options
        if o.sample_rate < 1.0 and random.random() >= o.sample_rate:
            return None
        self._defaults(event)
        event["contexts"].setdefault("trace", trace_context_for_event())
        event = _scope.apply_scopes(event, hint)  # type: ignore[assignment]
        if event is None:
            return None
        crumbs = event.get("breadcrumbs", {}).get("values")
        if crumbs and len(crumbs) > o.max_breadcrumbs:
            event["breadcrumbs"]["values"] = crumbs[-o.max_breadcrumbs:]
        self._privacy(event)
        # User-supplied data gets the tight limits; the event's own structure
        # (frames, breadcrumbs) is already bounded where it is built.
        for key in ("extra", "contexts", "user", "request"):
            if key in event:
                event[key] = to_json(event[key])
        event = to_json(event, depth=16, string_limit=16384, max_items=1000)
        self.scrubber.scrub_event(event)
        if o.before_send is not None:
            event = o.before_send(event, hint)
        return event

    def _privacy(self, event: Dict[str, Any]) -> None:
        if self.options.send_default_pii:
            return
        user = event.get("user")
        if isinstance(user, dict) and user.get("ip_address") == "{{auto}}":
            user.pop("ip_address")
        req = event.get("request")
        if isinstance(req, dict):
            req.pop("cookies", None)
            headers = req.get("headers")
            if isinstance(headers, dict):
                for h in list(headers):
                    if h.lower() in ("cookie", "set-cookie", "authorization", "proxy-authorization", "x-forwarded-for",
                                     "x-real-ip", "forwarded"):
                        headers.pop(h)
            req.pop("env", None)

    def _ignored(self, error: BaseException) -> bool:
        for rule in self.options.ignore_errors:
            if isinstance(rule, type) and isinstance(error, rule):
                return True
            if isinstance(rule, str) and (type(error).__name__ == rule or
                                          f"{type(error).__module__}.{type(error).__qualname__}" == rule):
                return True
        return False

    # --- breadcrumbs -----------------------------------------------------------------

    def add_breadcrumb(self, crumb: Dict[str, Any], hint: Optional[dict] = None) -> None:
        if not self.active:
            return
        crumb = dict(crumb)
        crumb.setdefault("timestamp", time.time())
        crumb.setdefault("type", "default")
        if self.options.before_breadcrumb is not None:
            try:
                crumb = self.options.before_breadcrumb(crumb, hint or {})  # type: ignore[assignment]
            except Exception:  # noqa: BLE001
                log.debug("errorlens: before_breadcrumb raised", exc_info=True)
            if crumb is None:
                return
        _scope.get_isolation_scope().add_breadcrumb(crumb)

    # --- transactions ----------------------------------------------------------------

    def start_transaction(self, name: str, op: str = "function", trace_id: Optional[str] = None,
                          parent_span_id: Optional[str] = None, parent_sampled: Optional[bool] = None,
                          context: Optional[Dict[str, Any]] = None) -> Transaction:
        sampled = self.sample_transaction(name, op, parent_sampled, context or {})
        return Transaction(name, op, self, trace_id, parent_span_id, sampled, context)

    def capture_transaction(self, tx: Transaction) -> None:
        if not self.active:
            return
        try:
            event = tx.to_event()
            self._defaults(event)
            event = _scope.apply_scopes(event, {})  # type: ignore[assignment]
            if event is None:
                return
            self._privacy(event)
            for key in ("extra", "contexts", "user", "request"):
                if key in event:
                    event[key] = to_json(event[key])
            event = to_json(event, depth=16, string_limit=16384, max_items=1000)
            self.scrubber.scrub_event(event)
            for span in event.get("spans", []):
                if isinstance(span.get("description"), str):
                    span["description"] = self.scrubber.scrub_string(span["description"])
            if self.options.before_send_transaction is not None:
                event = self.options.before_send_transaction(event, {})
                if event is None:
                    return
            if self.transport:
                self.transport.submit([({"type": "transaction"}, json_bytes(event))], event["event_id"])
        except Exception:  # noqa: BLE001
            log.debug("errorlens: could not send transaction", exc_info=True)

    # --- logs ------------------------------------------------------------------------

    def capture_log(self, level: str, body: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        if not (self.active and self.options.enable_logs):
            return
        level = {"warning": "warn", "critical": "fatal", "exception": "error"}.get(level, level)
        if level not in LOG_SEVERITY:
            level = "info"
        attrs: Dict[str, Any] = dict(attributes or {})
        if self.options.release:
            attrs["errorlens.release"] = self.options.release
        attrs["errorlens.environment"] = self.options.environment
        if self.options.server_name:
            attrs.setdefault("server.address", self.options.server_name)
        record: Optional[Dict[str, Any]] = {
            "timestamp": time.time(),
            "trace_id": trace_context_for_event()["trace_id"],
            "level": level,
            "severity_number": LOG_SEVERITY[level],
            "body": self.scrubber.scrub_string(str(body))[:8192],
            "attributes": attrs,
        }
        if self.options.before_send_log is not None:
            record = self.options.before_send_log(record)  # type: ignore[arg-type]
            if record is None:
                return
        record["attributes"] = {k: self._log_attr(k, v) for k, v in record["attributes"].items()}
        with self._logs_lock:
            self._logs.append(record)
            full = len(self._logs) >= 100
            if not full and self._logs_timer is None:
                self._logs_timer = threading.Timer(5.0, self.flush_logs)
                self._logs_timer.daemon = True
                self._logs_timer.start()
        if full:
            self.flush_logs()

    def _log_attr(self, key: str, value: Any) -> Dict[str, Any]:
        if self.scrubber.sensitive_key(key):
            return {"value": "[Filtered]", "type": "string"}
        if isinstance(value, bool):
            return {"value": value, "type": "boolean"}
        if isinstance(value, int):
            return {"value": value, "type": "integer"}
        if isinstance(value, float):
            return {"value": value, "type": "double"}
        return {"value": self.scrubber.scrub_string(str(value))[:1024], "type": "string"}

    def flush_logs(self) -> None:
        with self._logs_lock:
            batch, self._logs = self._logs, []
            if self._logs_timer is not None:
                self._logs_timer.cancel()
                self._logs_timer = None
        if batch and self.transport:
            self.transport.submit([({"type": "log", "item_count": len(batch)}, json_bytes({"items": batch}))])

    # --- cron check-ins --------------------------------------------------------------

    def capture_checkin(self, monitor_slug: str, status: str, check_in_id: Optional[str] = None,
                        duration: Optional[float] = None, monitor_config: Optional[Dict[str, Any]] = None) -> Optional[str]:
        if not self.active:
            return None
        check_in_id = check_in_id or uuid.uuid4().hex
        payload: Dict[str, Any] = {"check_in_id": check_in_id, "monitor_slug": monitor_slug, "status": status,
                                   "environment": self.options.environment}
        if duration is not None:
            payload["duration"] = duration
        if monitor_config:
            payload["monitor_config"] = monitor_config
        if self.options.release:
            payload["release"] = self.options.release
        if self.transport:
            self.transport.submit([({"type": "check_in"}, json_bytes(payload))])
        return check_in_id

    # --- lifecycle -------------------------------------------------------------------

    def flush(self, timeout: Optional[float] = None) -> bool:
        self.flush_logs()
        if not self.transport:
            return True
        return self.transport.flush(self.options.shutdown_timeout if timeout is None else timeout)

    def close(self, timeout: Optional[float] = None) -> None:
        self.flush(timeout)
        if self.transport:
            self.transport.close(0.1)
        self.active = False

    def _atexit(self) -> None:
        if self.active:
            self.flush()
