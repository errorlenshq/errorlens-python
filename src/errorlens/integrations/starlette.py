"""Starlette and FastAPI: request scope and transaction per request, route
names ("GET /items/{item_id}") and errors."""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import Integration
from .asgi import ErrorLensAsgiMiddleware


def _route_name(app: Any, scope: Dict[str, Any]) -> Optional[str]:
    route = scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    try:
        from starlette.routing import Match

        for r in getattr(app, "routes", []):
            match, _ = r.matches(scope)
            if match == Match.FULL:
                return getattr(r, "path", None)
    except Exception:  # noqa: BLE001
        pass
    return None


class StarletteIntegration(Integration):
    identifier = "starlette"

    def setup(self, _client) -> None:
        from starlette.applications import Starlette

        real_call = Starlette.__call__

        async def __call__(self: Any, scope: Any, receive: Any, send: Any) -> None:
            mw = self.__dict__.get("_errorlens_mw")
            if mw is None:
                async def app(s: Any, r: Any, se: Any) -> None:
                    await real_call(self, s, r, se)
                mw = ErrorLensAsgiMiddleware(app, route_name=lambda sc: _route_name(self, sc))
                self.__dict__["_errorlens_mw"] = mw
            await mw(scope, receive, send)

        Starlette.__call__ = __call__  # type: ignore[method-assign]
