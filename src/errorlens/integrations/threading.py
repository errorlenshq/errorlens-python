"""Uncaught exceptions in threads."""

from __future__ import annotations

import threading

from . import Integration
from ._util import client


class ThreadingIntegration(Integration):
    identifier = "threading"

    def setup(self, _client) -> None:
        previous = threading.excepthook

        def hook(args):  # type: ignore[no-untyped-def]
            c = client()
            if c is not None and args.exc_value is not None and not issubclass(args.exc_type, SystemExit):
                c.capture_exception(args.exc_value, mechanism={"type": "threading", "handled": False})
            previous(args)

        threading.excepthook = hook
