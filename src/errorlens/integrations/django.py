"""Django: request scope and transaction per request, URL route names,
errors (from got_request_exception) and SQL query spans."""

from __future__ import annotations

import sys
from typing import Any

from .. import scope as _scope
from ..tracing import start_span
from . import Integration
from ._util import client
from .asgi import ErrorLensAsgiMiddleware
from .wsgi import ErrorLensWsgiMiddleware


class DjangoIntegration(Integration):
    identifier = "django"

    def setup(self, _client) -> None:
        from django.core import signals
        from django.core.handlers import base, wsgi

        real_wsgi_call = wsgi.WSGIHandler.__call__

        def wsgi_call(self: Any, environ: Any, start_response: Any) -> Any:
            mw = self.__dict__.get("_errorlens_mw")
            if mw is None:
                mw = ErrorLensWsgiMiddleware(lambda e, s: real_wsgi_call(self, e, s))
                self.__dict__["_errorlens_mw"] = mw
            return mw(environ, start_response)

        wsgi.WSGIHandler.__call__ = wsgi_call  # type: ignore[method-assign]

        try:
            from django.core.handlers import asgi

            real_asgi_call = asgi.ASGIHandler.__call__

            async def asgi_call(self: Any, scope: Any, receive: Any, send: Any) -> Any:
                mw = self.__dict__.get("_errorlens_mw")
                if mw is None:
                    async def app(s: Any, r: Any, se: Any) -> Any:
                        return await real_asgi_call(self, s, r, se)
                    mw = ErrorLensAsgiMiddleware(app)
                    self.__dict__["_errorlens_mw"] = mw
                return await mw(scope, receive, send)

            asgi.ASGIHandler.__call__ = asgi_call  # type: ignore[method-assign]
        except ImportError:
            pass

        # Route names: "GET /orders/<int:id>/" instead of every concrete URL.
        real_resolve = getattr(base.BaseHandler, "resolve_request", None)
        if real_resolve is not None:
            def resolve_request(self: Any, request: Any) -> Any:
                match = real_resolve(self, request)
                try:
                    route = getattr(match, "route", None) or getattr(match, "view_name", None)
                    if route:
                        route = route if route.startswith("/") else "/" + route
                        _scope.get_isolation_scope().transaction_name = f"{request.method} {route}"
                except Exception:  # noqa: BLE001
                    pass
                return match
            base.BaseHandler.resolve_request = resolve_request  # type: ignore[method-assign]

        def got_exception(sender: Any, request: Any = None, **_: Any) -> None:
            c = client()
            exc = sys.exc_info()[1]
            if c is None or exc is None:
                return
            if request is not None:
                user = getattr(request, "user", None)
                if c.options.send_default_pii and user is not None and getattr(user, "is_authenticated", False):
                    _scope.get_isolation_scope().set_user({"id": str(getattr(user, "pk", "")),
                                                           "username": str(getattr(user, "get_username", lambda: "")())})
            c.capture_exception(exc, mechanism={"type": "django", "handled": False})

        signals.got_request_exception.connect(got_exception, weak=False, dispatch_uid="errorlens")

        # SQL spans.
        from django.db.backends import utils

        for name in ("execute", "executemany"):
            real = getattr(utils.CursorWrapper, name)

            def make(real: Any) -> Any:
                def wrapped(self: Any, sql: Any, params: Any = None) -> Any:
                    with start_span("db.sql.query", str(sql)[:1000]) as span:
                        span.set_data("db.system", getattr(self.db, "vendor", "sql"))
                        return real(self, sql, params)
                return wrapped

            setattr(utils.CursorWrapper, name, make(real))
