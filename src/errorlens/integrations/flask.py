"""Flask: request scope and transaction per request, route names, errors.

Flask turns exceptions into 500 responses itself, so errors are taken from its
got_request_exception signal.
"""

from __future__ import annotations

from typing import Any

from .. import scope as _scope
from . import Integration
from ._util import client
from .wsgi import ErrorLensWsgiMiddleware


class FlaskIntegration(Integration):
    identifier = "flask"

    def setup(self, _client) -> None:
        import flask
        from flask import signals

        real_call = flask.Flask.__call__

        def __call__(self: Any, environ: Any, start_response: Any) -> Any:
            mw = self.__dict__.get("_errorlens_mw")
            if mw is None:
                mw = ErrorLensWsgiMiddleware(lambda e, s: real_call(self, e, s))
                self.__dict__["_errorlens_mw"] = mw
            return mw(environ, start_response)

        flask.Flask.__call__ = __call__  # type: ignore[method-assign]

        def request_started(sender: Any, **_: Any) -> None:
            req = flask.request
            rule = getattr(req, "url_rule", None)
            if rule is not None:
                _scope.get_isolation_scope().transaction_name = f"{req.method} {rule.rule}"
            c = client()
            if c is not None and c.options.send_default_pii:
                def add_body(event: dict, hint: dict) -> dict:
                    try:
                        if req.is_json:
                            event.setdefault("request", {})["data"] = req.get_json(silent=True)
                        elif req.form:
                            event.setdefault("request", {})["data"] = req.form.to_dict()
                    except Exception:  # noqa: BLE001
                        pass
                    return event
                _scope.get_isolation_scope().add_event_processor(add_body)

        def got_exception(sender: Any, exception: BaseException, **_: Any) -> None:
            c = client()
            if c is not None:
                c.capture_exception(exception, mechanism={"type": "flask", "handled": False})

        signals.request_started.connect(request_started, weak=False)
        signals.got_request_exception.connect(got_exception, weak=False)
