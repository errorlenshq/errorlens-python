"""Client-side scrubbing: secrets are removed before an event leaves the app.

The ErrorLens server scrubs every event again; doing it here as well means a
secret never even crosses the network.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

FILTERED = "[Filtered]"

# Matched as substrings of the normalized key (lower case, "-" and "_" removed).
DEFAULT_KEYS = (
    "password", "passwd", "pwd", "secret", "token", "apikey", "accesskey", "auth",
    "credential", "cookie", "session", "csrf", "xsrf", "privatekey", "creditcard",
    "cardnumber", "cvv", "cvc", "ssn", "iban", "signature",
)

_VALUE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),  # JWT
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
]
_URL_PASSWORD = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)")
# key=value / key: "value" pairs, e.g. in log lines, messages or source code.
_KV_SECRET = re.compile(r"""(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s&,;]+)""")


def _norm(key: str) -> str:
    return key.lower().replace("-", "").replace("_", "")


class Scrubber:
    def __init__(self, extra_keys: Iterable[str] = (), safe_keys: Iterable[str] = ()):
        self.keys = tuple(DEFAULT_KEYS) + tuple(_norm(k) for k in extra_keys)
        self.safe = {k.lower() for k in safe_keys}

    def sensitive_key(self, key: Any) -> bool:
        if not isinstance(key, str) or key.lower() in self.safe:
            return False
        n = _norm(key)
        return any(k in n for k in self.keys)

    def scrub_string(self, text: str) -> str:
        if not text or len(text) < 8:
            return text
        for p in _VALUE_PATTERNS:
            text = p.sub(FILTERED, text)
        text = _URL_PASSWORD.sub(r"\1" + FILTERED + r"\3", text)
        return _KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + FILTERED, text)

    def scrub(self, value: Any, depth: int = 0) -> Any:
        """Recursively scrub dicts/lists/strings (in place for containers)."""
        if depth > 10:
            return value
        if isinstance(value, str):
            return self.scrub_string(value)
        if isinstance(value, dict):
            for k in list(value):
                if self.sensitive_key(k):
                    if value[k] not in (None, "", FILTERED):
                        value[k] = FILTERED
                else:
                    value[k] = self.scrub(value[k], depth + 1)
            return value
        if isinstance(value, list):
            for i, v in enumerate(value):
                value[i] = self.scrub(v, depth + 1)
            return value
        return value

    def scrub_event(self, event: dict) -> dict:
        for field in ("request", "extra", "contexts", "user", "tags", "breadcrumbs", "exception", "logentry"):
            if field in event:
                event[field] = self.scrub(event[field])
        if isinstance(event.get("message"), str):
            event["message"] = self.scrub_string(event["message"])
        return event
