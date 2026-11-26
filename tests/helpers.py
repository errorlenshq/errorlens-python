"""A fake ErrorLens server that records what the SDK sends."""

from __future__ import annotations

import gzip
import http.server
import json
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

import errorlens
from errorlens import scope as _scope


def parse_envelope(body: bytes) -> Tuple[Dict[str, Any], List[Tuple[Dict[str, Any], Any]]]:
    header_line, _, rest = body.partition(b"\n")
    header = json.loads(header_line)
    items = []
    while rest.strip():
        item_header_line, _, rest = rest.partition(b"\n")
        item_header = json.loads(item_header_line)
        n = item_header["length"]
        payload, rest = rest[:n], rest[n:]
        rest = rest[1:] if rest.startswith(b"\n") else rest
        items.append((item_header, json.loads(payload)))
    return header, items


class FakeServer:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.status = 200
        self.response_headers: Dict[str, str] = {}
        self.delay = 0.0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if server.delay:
                    time.sleep(server.delay)
                header, items = parse_envelope(raw)
                server.requests.append({"path": self.path, "headers": dict(self.headers), "raw": raw,
                                        "envelope_header": header, "items": items})
                self.send_response(server.status)
                for k, v in server.response_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b'{"id":"x"}')

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def dsn(self) -> str:
        return f"http://publickey123@127.0.0.1:{self.port}/42"

    def items(self, kind: Optional[str] = None) -> List[Any]:
        return [p for r in self.requests for h, p in r["items"] if kind is None or h["type"] == kind]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class SdkTestCase(unittest.TestCase):
    """Starts a fake server and a fresh SDK for each test."""

    init_options: Dict[str, Any] = {}

    def setUp(self) -> None:
        _scope.reset_for_tests()
        self.server = FakeServer()
        self.client = errorlens.init(self.server.dsn, release="test@1.0.0", environment="test", **self.init_options)

    def tearDown(self) -> None:
        errorlens.close(timeout=2)
        self.server.close()

    def flush(self) -> None:
        self.assertTrue(errorlens.flush(5), "flush timed out")

    def events(self) -> List[Dict[str, Any]]:
        self.flush()
        return self.server.items("event")

    def event(self) -> Dict[str, Any]:
        evs = self.events()
        self.assertEqual(len(evs), 1, f"expected one event, got {len(evs)}")
        return evs[0]
