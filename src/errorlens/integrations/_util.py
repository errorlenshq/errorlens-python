"""Helpers shared by integrations."""

from __future__ import annotations

import threading
from typing import Optional

_local = threading.local()


def client():
    """The active client, or None. Integrations always look it up at call time,
    so re-running init() (or close()) takes effect without re-patching."""
    from .. import get_client

    c = get_client()
    return c if c is not None and c.active else None


def in_sdk() -> bool:
    """True inside the SDK's own code paths (prevents feedback loops)."""
    return getattr(_local, "depth", 0) > 0 or threading.current_thread().name == "errorlens.transport"


class sdk_section:
    def __enter__(self) -> None:
        _local.depth = getattr(_local, "depth", 0) + 1

    def __exit__(self, *exc: object) -> None:
        _local.depth -= 1


def is_own_host(host: Optional[str], port: Optional[int] = None, scheme: str = "http") -> bool:
    """True for requests to the ErrorLens server itself (same host and port)."""
    c = client()
    if not (c and c.dsn and host):
        return False
    own_host, _, own_port = c.dsn.host.rpartition(":") if c.dsn.host.count(":") == 1 else (c.dsn.host, "", "")
    own_port_n = int(own_port) if own_port.isdigit() else (443 if c.dsn.scheme == "https" else 80)
    port = port or (443 if scheme == "https" else 80)
    return host.strip("[]").lower() == own_host.strip("[]").lower() and port == own_port_n
