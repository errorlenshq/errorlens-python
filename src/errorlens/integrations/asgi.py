"""ASGI middleware: per-request scope, trace and error reporting.

The Starlette/FastAPI and Django integrations install it automatically; for any
other ASGI app:

    from errorlens.integrations.asgi import ErrorLensAsgiMiddleware
    app = ErrorLensAsgiMiddleware(app)
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .. import scope as _scope
from ._util import client
from .wsgi import begin_request, capture_request_error, end_request, request_info


def asgi_headers(scope: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or []:
        key = raw_key.decode("latin-1").title()
        value = raw_value.decode("latin-1")
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


def asgi_url(scope: Dict[str, Any], headers: Dict[str, str]) -> str:
    scheme = scope.get("scheme", "http")
    host = headers.get("Host")
    if not host:
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else "localhost"
    return f"{scheme}://{host}{scope.get('root_path', '')}{scope.get('path', '')}"


class ErrorLensAsgiMiddleware:
    def __init__(self, app: Callable[..., Any], route_name: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None) -> None:
        self.app = app
        self.route_name = route_name

    async def __call__(self, scope: Dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") not in ("http", "websocket") or client() is None or scope.get("errorlens.handled"):
            await self.app(scope, receive, send)
            return
        scope["errorlens.handled"] = True
        with _scope.isolation_scope(fresh_breadcrumbs=True):
            c = client()
            headers = asgi_headers(scope)
            method = scope.get("method", "WEBSOCKET")
            path = scope.get("path", "/") or "/"
            client_addr = scope.get("client")
            info = request_info(method, asgi_url(scope, headers), scope.get("query_string", b"").decode("latin-1"),
                                headers, client_addr[0] if client_addr else None, bool(c and c.options.send_default_pii))
            tx = begin_request(method, path, headers, info, op="http.server" if scope["type"] == "http" else "websocket.server")
            status: Dict[str, Optional[int]] = {"code": None}

            async def _send(message: Dict[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    status["code"] = message.get("status")
                await send(message)

            try:
                await self.app(scope, receive, _send)
            except BaseException as e:  # noqa: BLE001
                capture_request_error(e, "asgi")
                status["code"] = status["code"] or 500
                raise
            finally:
                if self.route_name is not None:
                    try:
                        route = self.route_name(scope)
                    except Exception:  # noqa: BLE001
                        route = None
                    if route:
                        _scope.get_isolation_scope().transaction_name = f"{method} {route}"
                end_request(tx, status["code"] if scope["type"] == "http" else 200)
