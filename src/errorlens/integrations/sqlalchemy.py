"""SQLAlchemy: a span for every query inside a transaction."""

from __future__ import annotations

from typing import Any

from ..tracing import start_span
from . import Integration


class SqlAlchemyIntegration(Integration):
    identifier = "sqlalchemy"

    def setup(self, _client) -> None:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        def before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            span = start_span("db.sql.query", str(statement)[:1000])
            span.__enter__()
            span.set_data("db.system", conn.engine.dialect.name)
            conn.info.setdefault("_errorlens_spans", []).append(span)

        def after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            spans = conn.info.get("_errorlens_spans")
            if spans:
                spans.pop().__exit__(None, None, None)

        def on_error(context: Any) -> None:
            conn = getattr(context, "connection", None)
            spans = conn.info.get("_errorlens_spans") if conn is not None else None
            if spans:
                span = spans.pop()
                span.set_status("internal_error")
                span.__exit__(None, None, None)

        event.listen(Engine, "before_cursor_execute", before)
        event.listen(Engine, "after_cursor_execute", after)
        event.listen(Engine, "handle_error", on_error)
