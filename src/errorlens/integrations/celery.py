"""Celery: one isolated scope and transaction per task, errors from failed
tasks, and the trace carried from the code that queued the task."""

from __future__ import annotations

from typing import Any

from .. import scope as _scope
from ..tracing import continue_trace, new_trace_id, outgoing_traceparent
from . import Integration
from ._util import client


class CeleryIntegration(Integration):
    identifier = "celery"

    def __init__(self, propagate_traces: bool = True) -> None:
        self.propagate_traces = propagate_traces

    def setup(self, _client) -> None:
        from celery import signals

        integration = self

        def before_publish(headers: Any = None, **_: Any) -> None:
            if integration.propagate_traces and isinstance(headers, dict) and client() is not None:
                tp = outgoing_traceparent()
                if tp:
                    headers.setdefault("traceparent", tp)

        def prerun(task_id: Any = None, task: Any = None, **_: Any) -> None:
            c = client()
            if c is None or task is None:
                return
            iso = _scope.get_isolation_scope().fork()
            iso.clear_breadcrumbs()
            tokens = (_scope._isolation.set(iso), _scope._current.set(_scope.get_current_scope().fork()))
            request = getattr(task, "request", None)
            headers = {"traceparent": (getattr(request, "headers", None) or {}).get("traceparent")
                       or getattr(request, "traceparent", None) or ""}
            trace_id, parent, sampled = continue_trace(headers)
            iso.trace_id = trace_id or new_trace_id()
            iso.parent_span_id = parent
            iso.set_tag("celery.task", task.name)
            iso.set_context("celery", {"task": task.name, "id": str(task_id),
                                       "retries": getattr(request, "retries", 0)})
            tx = c.start_transaction(task.name, "queue.task.celery", iso.trace_id, parent, sampled)
            tx.__enter__()
            task.__dict__.setdefault("_errorlens", {})[task_id] = (tokens, tx)

        def postrun(task_id: Any = None, task: Any = None, state: Any = None, **_: Any) -> None:
            entry = getattr(task, "__dict__", {}).get("_errorlens", {}).pop(task_id, None)
            if entry is None:
                return
            (iso_token, cur_token), tx = entry
            tx.set_status("ok" if state in (None, "SUCCESS") else "internal_error")
            tx.__exit__(None, None, None)
            _scope._current.reset(cur_token)
            _scope._isolation.reset(iso_token)

        def failure(task_id: Any = None, exception: Any = None, sender: Any = None, **_: Any) -> None:
            c = client()
            if c is None or exception is None:
                return
            # Retries and ignored outcomes are control flow, not errors.
            name = type(exception).__name__
            if name in ("Retry", "Ignore", "Reject"):
                return
            c.capture_exception(exception, mechanism={"type": "celery", "handled": False})

        signals.before_task_publish.connect(before_publish, weak=False)
        signals.task_prerun.connect(prerun, weak=False)
        signals.task_postrun.connect(postrun, weak=False)
        signals.task_failure.connect(failure, weak=False)
