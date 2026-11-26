"""WSGI middleware: per-request scope, trace and error reporting.

Framework integrations (Django, Flask) install it automatically; for any other
WSGI app:

    from errorlens.integrations.wsgi import ErrorLensWsgiMiddleware
    app = ErrorLensWsgiMiddleware(app)
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Dict, Iterable, Iterator, Optional
from urllib.parse import quote

from .. import scope as _scope
from ..tracing import Transaction, continue_trace
from ._util import client

_HEADER_ALLOW_PII = {"cookie", "x-forwarded-for", "x-real-ip", "forwarded"}


def wsgi_headers(environ: Dict[str, Any]) -> Dict[str, str]:
    headers = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").title()] = str(value)
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH") and value:
            headers[key.replace("_", "-").title()] = str(value)
    return headers


def wsgi_url(environ: Dict[str, Any]) -> str:
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME", "")
    if not environ.get("HTTP_HOST"):
        port = str(environ.get("SERVER_PORT", ""))
        if port and not ((scheme == "https" and port == "443") or (scheme == "http" and port == "80")):
            host += ":" + port
    path = quote(environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", ""), safe="/:@!$&'()*+,;=")
    return f"{scheme}://{host}{path}"


def request_info(method: str, url: str, query: str, headers: Dict[str, str], remote: Optional[str], pii: bool) -> Dict[str, Any]:
    info: Dict[str, Any] = {"method": method, "url": url, "query_string": query,
                            "headers": {k: v for k, v in headers.items() if pii or k.lower() not in _HEADER_ALLOW_PII}}
    if pii and remote:
        info["env"] = {"REMOTE_ADDR": remote}
    return info


def begin_request(method: str, path: str, headers: Dict[str, str], request: Dict[str, Any], op: str = "http.server") -> Optional[Transaction]:
    """Set up the (already isolated) scope for a request and start its transaction."""
    c = client()
    iso = _scope.get_isolation_scope()
    iso.request = request
    trace_id, parent_span_id, parent_sampled = continue_trace(headers)
    from ..tracing import new_trace_id

    iso.trace_id = trace_id or new_trace_id()
    iso.parent_span_id = parent_span_id
    if c is None:
        return None
    if c.options.send_default_pii and request.get("env", {}).get("REMOTE_ADDR"):
        iso.set_user(dict(iso.user or {}, ip_address=request["env"]["REMOTE_ADDR"]))
    tx = c.start_transaction(f"{method} {path}", op, iso.trace_id, parent_span_id, parent_sampled,
                             {"request": request})
    tx.set_data("http.request.method", method)
    tx.set_data("url.path", path)
    tx.__enter__()
    return tx


def end_request(tx: Optional[Transaction], status: Optional[int]) -> None:
    if tx is None:
        return
    iso = _scope.get_isolation_scope()
    if iso.transaction_name:
        tx.set_name(iso.transaction_name)
    if status is not None:
        tx.set_http_status(status)
    elif tx.status is None:
        tx.set_status("internal_error")
    tx.__exit__(None, None, None)


def capture_request_error(exc: BaseException, mechanism: str) -> None:
    # Cancellation (client went away), SystemExit and KeyboardInterrupt are not errors.
    if not isinstance(exc, Exception):
        return
    c = client()
    if c is not None:
        c.capture_exception(exc, mechanism={"type": mechanism, "handled": False})


class _Response(Iterable[bytes]):
    """Runs the rest of the request (streaming included) in the request's context."""

    def __init__(self, ctx: contextvars.Context, iterable: Iterable[bytes], finish: Callable[[], None]):
        self._ctx = ctx
        self._iterable = iterable
        self._finish = finish
        self._closed = False
        self._finished = False

    def _done(self) -> None:
        if not self._finished:
            self._finished = True
            self._ctx.run(self._finish)

    def __iter__(self) -> Iterator[bytes]:
        iterator = self._ctx.run(iter, self._iterable)
        while True:
            try:
                chunk = self._ctx.run(next, iterator)
            except StopIteration:
                # Fully sent. Some servers and test clients never call close(), so the request ends here too.
                self._done()
                return
            except BaseException as e:  # noqa: BLE001
                if not isinstance(e, GeneratorExit):
                    self._ctx.run(capture_request_error, e, "wsgi")
                raise
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._iterable, "close", None)
            if close is not None:
                self._ctx.run(close)
        finally:
            self._done()


class ErrorLensWsgiMiddleware:
    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    def __call__(self, environ: Dict[str, Any], start_response: Callable[..., Any]) -> Any:
        if client() is None or environ.get("errorlens.handled"):
            return self.app(environ, start_response)
        environ["errorlens.handled"] = True
        ctx = contextvars.copy_context()
        return ctx.run(self._run, ctx, environ, start_response)

    def _run(self, ctx: contextvars.Context, environ: Dict[str, Any], start_response: Callable[..., Any]) -> Any:
        iso = _scope.get_isolation_scope().fork()
        iso.clear_breadcrumbs()
        _scope._isolation.set(iso)
        _scope._current.set(_scope.get_current_scope().fork())
        c = client()
        headers = wsgi_headers(environ)
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "") or "/"
        info = request_info(method, wsgi_url(environ), environ.get("QUERY_STRING", ""), headers,
                            environ.get("REMOTE_ADDR"), bool(c and c.options.send_default_pii))
        tx = begin_request(method, path, headers, info)
        status: Dict[str, Optional[int]] = {"code": None}

        def _start_response(status_line: str, response_headers: Any, exc_info: Any = None) -> Any:
            try:
                status["code"] = int(status_line.split(" ", 1)[0])
            except ValueError:
                pass
            return start_response(status_line, response_headers, exc_info) if exc_info else start_response(status_line, response_headers)

        try:
            result = self.app(environ, _start_response)
        except BaseException as e:  # noqa: BLE001
            capture_request_error(e, "wsgi")
            end_request(tx, 500)
            raise
        return _Response(ctx, result, lambda: end_request(tx, status["code"]))
