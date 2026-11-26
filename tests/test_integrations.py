from __future__ import annotations

import http.server
import logging
import threading
import unittest
import urllib.request
from typing import Any, Dict, List

import errorlens
from errorlens.integrations.wsgi import ErrorLensWsgiMiddleware

from helpers import SdkTestCase


class Upstream:
    """A plain HTTP service that records the traceparent it receives."""

    def __init__(self) -> None:
        self.seen: List[str] = []
        upstream = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                upstream.seen.append(self.headers.get("traceparent", ""))
                self.send_response(200 if self.path != "/fail" else 503)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def tx_named(server: Any, name: str) -> Dict[str, Any]:
    matches = [t for t in server.items("transaction") if t["transaction"] == name]
    assert matches, f"no transaction {name!r}; got {[t['transaction'] for t in server.items('transaction')]}"
    return matches[-1]


class TracingTests(SdkTestCase):
    init_options = {"traces_sample_rate": 1.0}

    def setUp(self) -> None:
        super().setUp()
        self.upstream = Upstream()

    def tearDown(self) -> None:
        self.upstream.close()
        super().tearDown()

    def test_transaction_spans_and_linked_error(self) -> None:
        with errorlens.start_transaction("nightly-export", op="task") as tx:
            with errorlens.start_span("db.query", "load orders"):
                pass
            try:
                1 / 0
            except ZeroDivisionError:
                errorlens.capture_exception()
        self.flush()
        t = tx_named(self.server, "nightly-export")
        self.assertEqual(t["contexts"]["trace"]["op"], "task")
        self.assertEqual([s["op"] for s in t["spans"]], ["db.query"])
        self.assertEqual(t["spans"][0]["parent_span_id"], t["contexts"]["trace"]["span_id"])
        self.assertGreaterEqual(t["timestamp"], t["start_timestamp"])
        err = self.server.items("event")[0]
        self.assertEqual(err["contexts"]["trace"]["trace_id"], tx.trace_id)

    def test_outgoing_http_span_breadcrumb_and_traceparent(self) -> None:
        with errorlens.start_transaction("sync", op="task") as tx:
            urllib.request.urlopen(self.upstream.url + "/items?token=secret").read()
        errorlens.capture_message("after")
        self.flush()
        t = tx_named(self.server, "sync")
        span = t["spans"][0]
        self.assertEqual(span["op"], "http.client")
        self.assertEqual(span["description"], f"GET {self.upstream.url}/items")  # query string never recorded
        self.assertEqual(span["data"]["http.response.status_code"], 200)
        self.assertEqual(self.upstream.seen[0], f"00-{tx.trace_id}-{span['span_id']}-01")
        crumbs = self.server.items("event")[0]["breadcrumbs"]["values"]
        self.assertEqual(crumbs[-1]["data"], {"method": "GET", "url": f"{self.upstream.url}/items", "status_code": 200})

    def test_unsampled_does_not_send_transactions(self) -> None:
        self.client.options.traces_sample_rate = 0.0
        with errorlens.start_transaction("skipped"):
            pass
        self.flush()
        self.assertEqual(self.server.items("transaction"), [])

    def test_continues_incoming_trace(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        with errorlens.start_transaction("child", traceparent=tp):
            pass
        self.flush()
        trace = tx_named(self.server, "child")["contexts"]["trace"]
        self.assertEqual(trace["trace_id"], "0af7651916cd43dd8448eb211c80319c")
        self.assertEqual(trace["parent_span_id"], "b7ad6b7169203331")


class LogsAndMonitorTests(SdkTestCase):
    init_options = {"enable_logs": True}

    def setUp(self) -> None:
        super().setUp()
        self.log = logging.getLogger("shop.checkout")
        self.log.setLevel(logging.INFO)

    def test_logging_records_become_logs_breadcrumbs_and_events(self) -> None:
        self.log.info("Inventory low for %s", "SKU-1", extra={"warehouse": "berlin"})
        self.log.error("Payment failed for order %s with password=hunter2", 42)
        self.flush()
        logs = [r for batch in self.server.items("log") for r in batch["items"]]
        info = next(r for r in logs if r["body"] == "Inventory low for SKU-1")
        self.assertEqual(info["level"], "info")
        self.assertEqual(info["severity_number"], 9)
        self.assertEqual(info["attributes"]["warehouse"], {"value": "berlin", "type": "string"})
        self.assertEqual(info["attributes"]["message.template"]["value"], "Inventory low for %s")
        self.assertEqual(info["attributes"]["errorlens.environment"]["value"], "test")
        self.assertEqual(len(info["trace_id"]), 32)
        ev = self.server.items("event")[0]
        self.assertEqual(ev["level"], "error")
        self.assertNotIn("hunter2", ev["message"])
        self.assertEqual(ev["breadcrumbs"]["values"][-1]["message"], "Inventory low for SKU-1")

    def test_structured_logger(self) -> None:
        errorlens.logger.info("Order {order_id} paid", order_id=42, amount=9.5, express=True)
        self.flush()
        rec = self.server.items("log")[0]["items"][0]
        self.assertEqual(rec["body"], "Order 42 paid")
        self.assertEqual(rec["attributes"]["order_id"], {"value": 42, "type": "integer"})
        self.assertEqual(rec["attributes"]["amount"], {"value": 9.5, "type": "double"})
        self.assertEqual(rec["attributes"]["express"], {"value": True, "type": "boolean"})

    def test_cron_monitor(self) -> None:
        @errorlens.monitor("nightly-backup", schedule="0 2 * * *")
        def backup() -> str:
            return "done"

        @errorlens.monitor("sync")
        def broken() -> None:
            raise RuntimeError("no")

        self.assertEqual(backup(), "done")
        with self.assertRaises(RuntimeError):
            broken()
        self.flush()
        checkins = self.server.items("check_in")
        self.assertEqual([(c["monitor_slug"], c["status"]) for c in checkins],
                         [("nightly-backup", "in_progress"), ("nightly-backup", "ok"), ("sync", "in_progress"), ("sync", "error")])
        self.assertEqual(checkins[0]["check_in_id"], checkins[1]["check_in_id"])
        self.assertEqual(checkins[1]["monitor_config"]["schedule"], {"type": "crontab", "value": "0 2 * * *"})
        self.assertIn("duration", checkins[1])


def call_wsgi(app: Any, path: str, headers: Dict[str, str] = None) -> tuple:
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": "q=1", "SERVER_NAME": "shop.test",
               "SERVER_PORT": "80", "wsgi.url_scheme": "http", "wsgi.input": __import__("io").BytesIO(b""),
               "wsgi.errors": __import__("sys").stderr, "REMOTE_ADDR": "203.0.113.9",
               "HTTP_HOST": "shop.test", "HTTP_COOKIE": "session=abc", "HTTP_AUTHORIZATION": "Bearer abcdefghijkl"}
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v
    status = {}

    def start_response(s: str, h: Any, exc_info: Any = None) -> None:
        status["s"] = s

    body = app(environ, start_response)
    try:
        data = b"".join(body)
    finally:
        getattr(body, "close", lambda: None)()
    return status.get("s"), data


class WebTests(SdkTestCase):
    init_options = {"traces_sample_rate": 1.0}

    def test_plain_wsgi_middleware(self) -> None:
        def app(environ: Any, start_response: Any) -> Any:
            errorlens.set_tag("handler", "orders")
            raise KeyError("shipping_address")

        with self.assertRaises(KeyError):
            call_wsgi(ErrorLensWsgiMiddleware(app), "/orders", {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"})
        self.flush()
        ev = self.server.items("event")[0]
        self.assertEqual(ev["exception"]["values"][-1]["mechanism"], {"type": "wsgi", "handled": False})
        self.assertEqual(ev["request"]["url"], "http://shop.test/orders")
        self.assertEqual(ev["request"]["method"], "GET")
        self.assertNotIn("Cookie", ev["request"]["headers"])        # personal data off by default
        self.assertNotIn("Authorization", ev["request"]["headers"])
        self.assertEqual(ev["tags"]["handler"], "orders")
        self.assertEqual(ev["contexts"]["trace"]["trace_id"], "a" * 32)
        tx = tx_named(self.server, "GET /orders")
        self.assertEqual(tx["contexts"]["trace"]["status"], "internal_error")
        self.assertEqual(tx["contexts"]["trace"]["parent_span_id"], "b" * 16)

    def test_requests_do_not_share_context(self) -> None:
        def app(environ: Any, start_response: Any) -> Any:
            errorlens.set_tag("path", environ["PATH_INFO"])
            errorlens.add_breadcrumb(message=environ["PATH_INFO"])
            errorlens.capture_message("hi")
            start_response("200 OK", [])
            return [b"ok"]

        mw = ErrorLensWsgiMiddleware(app)
        call_wsgi(mw, "/a")
        call_wsgi(mw, "/b")
        self.flush()
        a, b = self.server.items("event")
        self.assertEqual((a["tags"]["path"], b["tags"]["path"]), ("/a", "/b"))
        self.assertEqual([c["message"] for c in b["breadcrumbs"]["values"]], ["/b"])

    def test_flask(self) -> None:
        import flask

        app = flask.Flask(__name__)

        @app.route("/items/<int:item_id>")
        def item(item_id: int) -> str:
            if item_id == 13:
                raise ValueError("unlucky")
            return "ok"

        self.assertEqual(call_wsgi(app, "/items/1")[0], "200 OK")
        self.assertEqual(call_wsgi(app, "/items/13")[0], "500 INTERNAL SERVER ERROR")
        self.flush()
        ev = self.server.items("event")[0]
        self.assertEqual(ev["exception"]["values"][-1]["mechanism"]["type"], "flask")
        self.assertEqual(ev["transaction"], "GET /items/<int:item_id>")
        ok = [t for t in self.server.items("transaction") if t["contexts"]["trace"]["status"] == "ok"]
        self.assertEqual(ok[0]["transaction"], "GET /items/<int:item_id>")

    def test_fastapi(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()

        @app.get("/items/{item_id}")
        async def item(item_id: int) -> dict:
            if item_id == 13:
                raise ValueError("unlucky")
            return {"id": item_id}

        client = TestClient(app, raise_server_exceptions=False)
        self.assertEqual(client.get("/items/1").status_code, 200)
        self.assertEqual(client.get("/items/13").status_code, 500)
        self.flush()
        ev = self.server.items("event")[0]
        self.assertEqual(ev["exception"]["values"][-1]["type"], "ValueError")
        self.assertEqual(ev["exception"]["values"][-1]["mechanism"]["type"], "asgi")
        names = {t["transaction"] for t in self.server.items("transaction")}
        self.assertEqual(names, {"GET /items/{item_id}"})

    def test_django(self) -> None:
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(DEBUG=False, ROOT_URLCONF=__name__ + "_urls", ALLOWED_HOSTS=["*"], SECRET_KEY="x" * 50,
                               MIDDLEWARE=[], DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
            import sys
            import types

            from django.http import HttpResponse
            from django.urls import path

            def view(request: Any, order_id: int) -> Any:
                from django.db import connection

                with connection.cursor() as c:
                    c.execute("SELECT %s", [order_id])
                if order_id == 13:
                    raise ValueError("unlucky")
                return HttpResponse("ok")

            urls = types.ModuleType(__name__ + "_urls")
            urls.urlpatterns = [path("orders/<int:order_id>/", view)]  # type: ignore[attr-defined]
            sys.modules[urls.__name__] = urls
            django.setup()
        from django.core.handlers.wsgi import WSGIHandler

        app = WSGIHandler()
        self.assertEqual(call_wsgi(app, "/orders/1/")[0], "200 OK")
        self.assertTrue(call_wsgi(app, "/orders/13/")[0].startswith("500"))
        self.flush()
        ev = self.server.items("event")[0]
        self.assertEqual(ev["exception"]["values"][-1]["mechanism"]["type"], "django")
        self.assertEqual(ev["transaction"], "GET /orders/<int:order_id>/")
        tx = tx_named(self.server, "GET /orders/<int:order_id>/")
        self.assertIn("db.sql.query", [s["op"] for s in tx["spans"]])


class LibraryTests(SdkTestCase):
    init_options = {"traces_sample_rate": 1.0}

    def test_celery_task_failure_and_transaction(self) -> None:
        from celery import Celery

        app = Celery("t", broker="memory://", backend="cache+memory://")
        app.conf.task_always_eager = True

        @app.task(name="orders.charge")
        def charge(order_id: int) -> None:
            raise RuntimeError(f"card declined for {order_id}")

        charge.apply(args=(7,))
        self.flush()
        ev = self.server.items("event")[0]
        self.assertEqual(ev["exception"]["values"][-1]["mechanism"]["type"], "celery")
        self.assertEqual(ev["tags"]["celery.task"], "orders.charge")
        tx = tx_named(self.server, "orders.charge")
        self.assertEqual(tx["contexts"]["trace"]["op"], "queue.task.celery")
        self.assertEqual(tx["contexts"]["trace"]["status"], "internal_error")

    def test_httpx(self) -> None:
        import httpx

        upstream = Upstream()
        try:
            with errorlens.start_transaction("fetch") as tx:
                httpx.get(upstream.url + "/x")
            self.flush()
            span = tx_named(self.server, "fetch")["spans"][0]
            self.assertEqual(span["op"], "http.client")
            self.assertTrue(upstream.seen[0].startswith(f"00-{tx.trace_id}-"))
        finally:
            upstream.close()

    def test_sqlalchemy(self) -> None:
        from sqlalchemy import create_engine, text

        engine = create_engine("sqlite://")
        with errorlens.start_transaction("report"):
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        self.flush()
        spans = tx_named(self.server, "report")["spans"]
        self.assertEqual([s["description"] for s in spans if s["op"] == "db.sql.query"], ["SELECT 1"])


if __name__ == "__main__":
    unittest.main()
