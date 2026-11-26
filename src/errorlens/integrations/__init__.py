"""Integrations hook ErrorLens into the runtime and libraries.

Default integrations (uncaught exceptions, logging, outgoing HTTP) are always on.
Framework integrations switch on automatically when the framework is installed:
Django, Flask, FastAPI/Starlette, Celery, httpx and SQLAlchemy.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from ..client import Client

log = logging.getLogger("errorlens")


class Integration:
    identifier = "base"
    #: the module that must be importable for auto-enabling (None = always)
    requires: Any = None

    def setup(self, client: "Client") -> None:
        raise NotImplementedError


_installed: Dict[str, Integration] = {}

_DEFAULTS = [
    ("errorlens.integrations.excepthook", "ExceptHookIntegration"),
    ("errorlens.integrations.threading", "ThreadingIntegration"),
    ("errorlens.integrations.logging", "LoggingIntegration"),
    ("errorlens.integrations.stdlib_http", "StdlibHttpIntegration"),
]
_AUTO = [
    ("errorlens.integrations.django", "DjangoIntegration", "django"),
    ("errorlens.integrations.flask", "FlaskIntegration", "flask"),
    ("errorlens.integrations.starlette", "StarletteIntegration", "starlette"),
    ("errorlens.integrations.celery", "CeleryIntegration", "celery"),
    ("errorlens.integrations.httpx", "HttpxIntegration", "httpx"),
    ("errorlens.integrations.sqlalchemy", "SqlAlchemyIntegration", "sqlalchemy"),
]


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001 - a broken optional package must not break init
        return False


def setup_integrations(client: "Client") -> List[str]:
    o = client.options
    chosen: Dict[str, Integration] = {}
    if o.default_integrations:
        for module, cls in _DEFAULTS:
            integ = getattr(importlib.import_module(module), cls)()
            chosen[integ.identifier] = integ
        for module, cls, requires in _AUTO:
            if _importable(requires):
                integ = getattr(importlib.import_module(module), cls)()
                chosen[integ.identifier] = integ
    for integ in o.integrations:  # explicit ones override defaults (e.g. custom logging levels)
        chosen[integ.identifier] = integ
    for name in o.disabled_integrations:
        chosen.pop(name, None)

    names = []
    for ident, integ in chosen.items():
        if ident in _installed:  # patches are process-wide: install once, then just re-point
            _installed[ident].__dict__.update(integ.__dict__)
            names.append(ident)
            continue
        try:
            integ.setup(client)
            _installed[ident] = integ
            names.append(ident)
        except Exception:  # noqa: BLE001
            log.warning("errorlens: could not set up the %s integration", ident, exc_info=o.debug)
    return names
