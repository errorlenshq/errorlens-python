"""httpx (sync and async clients): breadcrumbs, spans and trace propagation."""

from __future__ import annotations

import time
from typing import Any

from ..tracing import outgoing_traceparent, start_span
from . import Integration
from ._util import client, in_sdk, is_own_host
from .stdlib_http import should_propagate, strip_query


def _before(request: Any) -> Any:
    if in_sdk() or client() is None or is_own_host(request.url.host, request.url.port, request.url.scheme):
        return None
    url = strip_query(str(request.url))
    span = start_span("http.client", f"{request.method} {url}")
    span.__enter__()
    span.set_data("http.request.method", request.method)
    span.set_data("url.full", url)
    if should_propagate(str(request.url)) and "traceparent" not in request.headers:
        tp = span.to_traceparent() if span.sampled else outgoing_traceparent()
        if tp:
            request.headers["traceparent"] = tp
    return span, request.method, url, time.time()


def _after(state: Any, status: Any) -> None:
    if state is None:
        return
    span, method, url, started = state
    if status is not None:
        span.set_http_status(status)
    else:
        span.set_status("internal_error")
    span.__exit__(None, None, None)
    c = client()
    if c is not None:
        c.add_breadcrumb({"type": "http", "category": "http", "timestamp": started,
                          "level": "error" if status is None or status >= 500 else "info",
                          "data": {"method": method, "url": url, "status_code": status}})


class HttpxIntegration(Integration):
    identifier = "httpx"

    def setup(self, _client) -> None:
        import httpx

        real_send = httpx.Client.send
        real_async_send = httpx.AsyncClient.send

        def send(self: Any, request: Any, **kwargs: Any) -> Any:
            state = _before(request)
            status = None
            try:
                response = real_send(self, request, **kwargs)
                status = response.status_code
                return response
            finally:
                _after(state, status)

        async def async_send(self: Any, request: Any, **kwargs: Any) -> Any:
            state = _before(request)
            status = None
            try:
                response = await real_async_send(self, request, **kwargs)
                status = response.status_code
                return response
            finally:
                _after(state, status)

        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = async_send  # type: ignore[method-assign]
