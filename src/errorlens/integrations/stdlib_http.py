"""Outgoing HTTP through http.client — urllib, requests and urllib3 included.

Each request becomes a breadcrumb and, inside a transaction, a span. The W3C
traceparent header is added so the receiving service continues the trace.
"""

from __future__ import annotations

import http.client
import re
import time
from typing import Any, Optional

from . import Integration
from ._util import client, in_sdk, is_own_host
from ..tracing import outgoing_traceparent, start_span


def should_propagate(url: str) -> bool:
    c = client()
    targets = c.options.trace_propagation_targets if c else None
    if targets is None:
        return True
    return any(re.search(t, url) for t in targets)


def strip_query(url: str) -> str:
    return url.split("?", 1)[0]


class StdlibHttpIntegration(Integration):
    identifier = "stdlib_http"

    def setup(self, _client) -> None:
        conn = http.client.HTTPConnection
        real_putrequest = conn.putrequest
        real_getresponse = conn.getresponse

        def putrequest(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            result = real_putrequest(self, method, url, *args, **kwargs)
            scheme = "https" if isinstance(self, http.client.HTTPSConnection) else "http"
            if in_sdk() or client() is None or is_own_host(self.host, self.port, scheme):
                return result
            port = "" if self.port in (80, 443, None) else f":{self.port}"
            full = url if url.startswith(("http://", "https://")) else f"{scheme}://{self.host}{port}{url}"
            span = start_span("http.client", f"{method} {strip_query(full)}")
            span.__enter__()
            span.set_data("http.request.method", method)
            span.set_data("url.full", strip_query(full))
            self._errorlens = (span, method, strip_query(full), time.time())
            if should_propagate(full):
                tp = span.to_traceparent() if span.sampled else outgoing_traceparent()
                if tp:
                    try:
                        self.putheader("traceparent", tp)
                    except Exception:  # noqa: BLE001
                        pass
            return result

        def getresponse(self: Any, *args: Any, **kwargs: Any) -> Any:
            state = getattr(self, "_errorlens", None)
            status: Optional[int] = None
            try:
                response = real_getresponse(self, *args, **kwargs)
                status = response.status
                return response
            finally:
                if state is not None:
                    self._errorlens = None
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

        conn.putrequest = putrequest  # type: ignore[method-assign]
        conn.getresponse = getresponse  # type: ignore[method-assign]
