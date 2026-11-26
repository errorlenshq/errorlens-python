"""Uncaught exceptions in the main thread."""

from __future__ import annotations

import sys

from . import Integration
from ._util import client


class ExceptHookIntegration(Integration):
    identifier = "excepthook"

    def setup(self, _client) -> None:
        previous = sys.excepthook

        def hook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
            c = client()
            if c is not None and exc is not None and not issubclass(exc_type, KeyboardInterrupt):
                c.capture_exception(exc, mechanism={"type": "excepthook", "handled": False}, level="fatal")
                c.flush()  # the process is about to exit
            previous(exc_type, exc, tb)

        sys.excepthook = hook
