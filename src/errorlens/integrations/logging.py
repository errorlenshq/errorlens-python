"""Python's logging module.

* records at ``level`` and above (default INFO) become breadcrumbs;
* records at ``event_level`` and above (default ERROR) are reported as events
  (with the exception, if logged with exc_info / logger.exception);
* with ``init(enable_logs=True)``, records at ``logs_level`` and above (default
  INFO) are also sent as structured logs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import Integration
from ._util import client, in_sdk, sdk_section

_IGNORED = {"errorlens", "urllib3.connectionpool"}
_STD_ATTRS = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName"}
_LEVELS = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning", logging.ERROR: "error",
           logging.CRITICAL: "fatal"}


def _level(record: logging.LogRecord) -> str:
    for threshold in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO):
        if record.levelno >= threshold:
            return _LEVELS[threshold]
    return "debug"


def _extra(record: logging.LogRecord) -> Dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _STD_ATTRS and not k.startswith("_")}


class ErrorLensHandler(logging.Handler):
    def __init__(self, integration: "LoggingIntegration") -> None:
        super().__init__(level=logging.NOTSET)
        self.integration = integration

    def emit(self, record: logging.LogRecord) -> None:
        if in_sdk() or record.name.split(".")[0] in _IGNORED or record.name in _IGNORED:
            return
        c = client()
        if c is None:
            return
        with sdk_section():
            try:
                self._handle(c, record)
            except Exception:  # noqa: BLE001
                pass

    def _handle(self, c, record: logging.LogRecord) -> None:  # type: ignore[no-untyped-def]
        i = self.integration
        message = record.getMessage()
        level = _level(record)
        extra = _extra(record)
        if i.logs_level is not None and record.levelno >= i.logs_level and c.options.enable_logs:
            attrs: Dict[str, Any] = {"logger.name": record.name, "code.function": record.funcName,
                                     "code.lineno": record.lineno, "code.filepath": record.pathname}
            if isinstance(record.msg, str) and record.args:
                attrs["message.template"] = record.msg
            attrs.update(extra)
            c.capture_log("warn" if level == "warning" else level, message, attrs)
        if i.event_level is not None and record.levelno >= i.event_level:
            if record.exc_info and record.exc_info[1] is not None:
                c.capture_exception(record.exc_info[1], mechanism={"type": "logging", "handled": True}, level=level)
            else:
                event: Dict[str, Any] = {"level": level, "logger": record.name,
                                         "logentry": {"message": record.msg if isinstance(record.msg, str) else message,
                                                      "formatted": message},
                                         "message": message}
                if extra:
                    event["extra"] = extra
                c.capture_event(event, {"log_record": record})
            return  # an event already carries everything; skip the breadcrumb
        if i.level is not None and record.levelno >= i.level:
            c.add_breadcrumb({"type": "log", "category": record.name, "level": level, "message": message,
                              "data": extra or None, "timestamp": record.created}, {"log_record": record})


class LoggingIntegration(Integration):
    identifier = "logging"

    def __init__(self, level: Optional[int] = logging.INFO, event_level: Optional[int] = logging.ERROR,
                 logs_level: Optional[int] = logging.INFO) -> None:
        self.level = level
        self.event_level = event_level
        self.logs_level = logs_level

    def setup(self, _client) -> None:
        # The app's own logger levels decide which records exist; the SDK never
        # changes them (that would change what the app's other handlers print).
        logging.getLogger().addHandler(ErrorLensHandler(self))
