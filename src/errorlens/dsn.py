"""DSN parsing.

A DSN looks like ``https://<public-key>@errorlens.io/<project-id>``. The key
only allows sending data to that project.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


class InvalidDsn(ValueError):
    """Raised for a DSN that can't be used."""


@dataclass(frozen=True)
class Dsn:
    scheme: str
    public_key: str
    host: str  # host[:port]
    path: str  # path prefix before the project id ("" or "/prefix")
    project_id: str

    @classmethod
    def parse(cls, value: str) -> "Dsn":
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https"):
            raise InvalidDsn("DSN must start with http:// or https://")
        if not parts.username:
            raise InvalidDsn("DSN is missing its public key (https://<key>@host/<project>)")
        if not parts.hostname:
            raise InvalidDsn("DSN is missing a host")
        path, _, project = parts.path.rstrip("/").rpartition("/")
        if not project.isdigit():
            raise InvalidDsn("DSN must end with a numeric project id")
        host = parts.hostname
        if ":" in host:  # IPv6 literal
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        return cls(parts.scheme, parts.username, host, path, project)

    @property
    def envelope_url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}/api/{self.project_id}/envelope/"

    def __str__(self) -> str:  # the key is not secret, but keep logs tidy
        return f"{self.scheme}://{self.public_key[:6]}…@{self.host}{self.path}/{self.project_id}"
