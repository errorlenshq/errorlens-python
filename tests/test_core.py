from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
import unittest

import errorlens
from errorlens.dsn import Dsn, InvalidDsn

from helpers import FakeServer, SdkTestCase


class DsnTests(unittest.TestCase):
    def test_parse(self) -> None:
        d = Dsn.parse("https://abc123@errorlens.io/7")
        self.assertEqual((d.scheme, d.public_key, d.host, d.project_id), ("https", "abc123", "errorlens.io", "7"))
        self.assertEqual(d.envelope_url, "https://errorlens.io/api/7/envelope/")
        d = Dsn.parse("http://k@localhost:8080/errors/12")
        self.assertEqual(d.envelope_url, "http://localhost:8080/errors/api/12/envelope/")

    def test_invalid(self) -> None:
        for bad in ("errorlens.io/1", "https://errorlens.io/1", "https://k@errorlens.io/", "ftp://k@h/1", "https://k@h/abc"):
            with self.assertRaises(InvalidDsn, msg=bad):
                Dsn.parse(bad)

    def test_no_dsn_is_a_noop(self) -> None:
        client = errorlens.init(None)
        self.assertFalse(client.active)
        self.assertIsNone(errorlens.capture_message("ignored"))
        errorlens.close()


def pricing(items: list) -> float:
    api_key = "sk_live_51Hx0123456789abcdefgh"  # noqa: F841 - must be filtered
    password = "hunter2"  # noqa: F841
    total = 10
    return total / len(items)


class ErrorTests(SdkTestCase):
    def test_exception_with_stack_source_and_locals(self) -> None:
        try:
            pricing([])
        except ZeroDivisionError:
            event_id = errorlens.capture_exception()
        ev = self.event()
        self.assertEqual(ev["event_id"], event_id)
        self.assertEqual(ev["level"], "error")
        self.assertEqual(ev["platform"], "python")
        self.assertEqual(ev["release"], "test@1.0.0")
        self.assertEqual(ev["environment"], "test")
        exc = ev["exception"]["values"][-1]
        self.assertEqual(exc["type"], "ZeroDivisionError")
        self.assertEqual(exc["value"], "division by zero")
        self.assertEqual(exc["mechanism"], {"type": "generic", "handled": True})
        frame = exc["stacktrace"]["frames"][-1]
        self.assertEqual(frame["function"], "pricing")
        self.assertTrue(frame["in_app"])
        self.assertEqual(frame["context_line"].strip(), "return total / len(items)")
        self.assertEqual(len(frame["pre_context"]), 5)
        self.assertEqual(frame["vars"]["total"], "10")
        # Secrets in local variables never leave the process.
        self.assertEqual(frame["vars"]["api_key"], "[Filtered]")
        self.assertEqual(frame["vars"]["password"], "[Filtered]")
        # ...and neither do secrets written in the source code around the error.
        self.assertNotIn("hunter2", json.dumps(exc))
        self.assertEqual(ev["sdk"]["name"], "errorlens.python")
        self.assertEqual(ev["contexts"]["runtime"]["name"], "CPython")
        self.assertEqual(len(ev["contexts"]["trace"]["trace_id"]), 32)

    def test_chained_exceptions_oldest_first(self) -> None:
        try:
            try:
                {}["missing"]
            except KeyError as e:
                raise ValueError("bad config") from e
        except ValueError as e:
            errorlens.capture_exception(e)
        values = self.event()["exception"]["values"]
        self.assertEqual([v["type"] for v in values], ["KeyError", "ValueError"])
        self.assertEqual(values[0]["mechanism"]["source"], "__cause__")

    def test_library_frames_are_not_in_app(self) -> None:
        try:
            json.loads("{not json")
        except ValueError as e:
            errorlens.capture_exception(e)
        frames = self.event()["exception"]["values"][-1]["stacktrace"]["frames"]
        self.assertTrue(frames[0]["in_app"])  # this test file
        self.assertFalse(frames[-1]["in_app"])  # the json module
        self.assertNotIn("vars", frames[-1])

    def test_message_scope_and_breadcrumbs(self) -> None:
        errorlens.set_user({"id": "42"})
        errorlens.set_tag("region", "eu")
        errorlens.set_context("order", {"id": 9})
        errorlens.set_extra("cart", [1, 2])
        errorlens.add_breadcrumb(category="auth", message="signed in")
        errorlens.capture_message("hello", level="warning")
        ev = self.event()
        self.assertEqual(ev["message"], "hello")
        self.assertEqual(ev["level"], "warning")
        self.assertEqual(ev["user"], {"id": "42"})
        self.assertEqual(ev["tags"]["region"], "eu")
        self.assertEqual(ev["contexts"]["order"], {"id": 9})
        self.assertEqual(ev["extra"]["cart"], [1, 2])
        self.assertEqual(ev["breadcrumbs"]["values"][-1]["message"], "signed in")

    def test_new_scope_is_temporary(self) -> None:
        with errorlens.new_scope() as scope:
            scope.set_tag("inside", "yes")
            errorlens.capture_message("one")
        errorlens.capture_message("two")
        one, two = self.events()
        self.assertEqual(one["tags"].get("inside"), "yes")
        self.assertNotIn("inside", two.get("tags", {}))

    def test_isolation_between_threads(self) -> None:
        def worker(n: int) -> None:
            with errorlens.isolation_scope():
                errorlens.set_tag("worker", str(n))
                time.sleep(0.02)
                errorlens.capture_message(f"from {n}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for ev in self.events():
            self.assertEqual(ev["message"], f"from {ev['tags']['worker']}")

    def test_scrubbing_of_strings(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlX2hlcmU"
        errorlens.capture_message(f"token was {jwt} and db postgres://app:s3cret@db/prod")
        msg = self.event()["message"]
        self.assertNotIn(jwt, msg)
        self.assertNotIn("s3cret", msg)
        self.assertIn("[Filtered]", msg)

    def test_before_send_ignore_and_sampling(self) -> None:
        self.client.options.before_send = lambda e, h: None if e.get("message") == "drop" else dict(e, tags={"seen": "1"})
        self.client.options.ignore_errors = [KeyError]
        errorlens.capture_message("drop")
        errorlens.capture_message("keep")
        errorlens.capture_exception(KeyError("x"))
        self.assertEqual([e["message"] for e in self.events()], ["keep"])
        self.client.options.sample_rate = 0.0
        errorlens.capture_message("sampled out")
        self.assertEqual(len(self.events()), 1)

    def test_nothing_mentions_another_vendor(self) -> None:
        try:
            pricing([])
        except ZeroDivisionError:
            errorlens.capture_exception()
        with errorlens.start_transaction("t"):
            pass
        self.flush()
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).lower().encode()
        vendor = "sen" + "try"  # another SDK vendor's name; split so it isn't in our package either
        for r in self.server.requests:
            # Ignore this machine's own file paths (the checkout may live anywhere).
            self.assertNotIn(vendor.encode(), r["raw"].lower().replace(here, b""))
            for k, v in r["headers"].items():
                self.assertNotIn(vendor, f"{k}:{v}".lower())
        headers = {k.lower(): v for k, v in self.server.requests[0]["headers"].items()}
        self.assertEqual(headers["x-errorlens-key"], "publickey123")


class TransportTests(SdkTestCase):
    def test_retry_after_pauses_sending(self) -> None:
        self.server.status = 429
        self.server.response_headers = {"Retry-After": "60"}
        errorlens.capture_message("first")
        self.flush()
        self.assertEqual(len(self.server.requests), 1)
        errorlens.capture_message("second")  # dropped while rate limited
        self.flush()
        self.assertEqual(len(self.server.requests), 1)

    def test_never_blocks_when_server_is_slow(self) -> None:
        self.server.delay = 0.5
        start = time.monotonic()
        for i in range(300):
            errorlens.capture_message(f"m{i}")
        # 300 requests at 0.5 s each would take 150 s if capturing waited for the network.
        self.assertLess(time.monotonic() - start, 5.0, "capturing must not wait for the network")
        self.assertGreater(self.client.transport.dropped, 0)  # bounded queue

    def test_unreachable_server_is_harmless(self) -> None:
        errorlens.init("http://k@127.0.0.1:9/1")
        errorlens.capture_message("nobody listens")
        self.assertTrue(errorlens.flush(5))


class ProcessExitTests(unittest.TestCase):
    def test_uncaught_exception_is_reported_before_exit(self) -> None:
        server = FakeServer()
        code = textwrap.dedent(f"""
            import errorlens
            errorlens.init("{server.dsn}")
            def main():
                raise RuntimeError("boom at exit")
            main()
        """)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                              env={"PYTHONPATH": ":".join(sys.path)})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RuntimeError: boom at exit", proc.stderr)  # normal traceback still printed
        events = server.items("event")
        self.assertEqual(len(events), 1)
        exc = events[0]["exception"]["values"][-1]
        self.assertEqual(exc["mechanism"], {"type": "excepthook", "handled": False})
        self.assertEqual(events[0]["level"], "fatal")
        server.close()


if __name__ == "__main__":
    unittest.main()
