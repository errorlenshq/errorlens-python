"""Sending envelopes to ErrorLens on a background thread.

The application never waits for the network: work goes into a bounded queue
and a daemon thread sends it. When the queue is full (the server is down or
the app produces more than it can send) new items are dropped rather than
growing memory. 429 responses pause sending for the time the server asks.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._version import __version__
from .dsn import Dsn

log = logging.getLogger("errorlens")

USER_AGENT = f"errorlens-python/{__version__}"

Item = Tuple[Dict[str, Any], bytes]  # (item header, payload)


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def build_envelope(items: List[Item], event_id: Optional[str] = None) -> bytes:
    header: Dict[str, Any] = {"sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "sdk": {"name": "errorlens.python", "version": __version__}}
    if event_id:
        header["event_id"] = event_id
    parts = [json_bytes(header)]
    for item_header, payload in items:
        h = dict(item_header)
        h["length"] = len(payload)
        parts.append(json_bytes(h))
        parts.append(payload)
    return b"\n".join(parts) + b"\n"


class Transport:
    """Base class; tests and custom deployments can replace send_envelope."""

    def submit(self, items: List[Item], event_id: Optional[str] = None) -> bool:
        raise NotImplementedError

    def flush(self, timeout: float) -> bool:
        return True

    def close(self, timeout: float) -> None:
        pass


class HttpTransport(Transport):
    def __init__(self, dsn: Dsn, queue_size: int = 100, timeout: float = 10.0,
                 proxy: Optional[str] = None, ca_certs: Optional[str] = None,
                 send: Optional[Callable[[bytes], Tuple[int, Dict[str, str]]]] = None):
        self.dsn = dsn
        self.timeout = timeout
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=queue_size)
        self._queue_size = queue_size
        self._disabled_until: Dict[str, float] = {}  # category ("" = all) -> monotonic time
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._pid = os.getpid()
        self._closed = False
        self._send_override = send
        self._opener = self._make_opener(proxy, ca_certs)
        self.dropped = 0

    # --- public ---------------------------------------------------------------------

    def submit(self, items: List[Item], event_id: Optional[str] = None) -> bool:
        if self._closed:
            return False
        items = [it for it in items if not self._is_limited(it[0].get("type", ""))]
        if not items:
            self.dropped += 1
            return False
        self._ensure_thread()
        try:
            self._queue.put_nowait(build_envelope(items, event_id))
            return True
        except queue.Full:
            self.dropped += 1
            log.debug("errorlens: queue full, dropping data")
            return False

    def flush(self, timeout: float) -> bool:
        """Wait until everything queued has been sent (or timeout passes)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def close(self, timeout: float) -> None:
        self.flush(timeout)
        self._closed = True
        thread = self._thread
        if thread and thread.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

    # --- worker ---------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        # After fork() the worker thread doesn't exist in the child: start anew.
        if self._pid != os.getpid():
            with self._lock:
                if self._pid != os.getpid():
                    self._pid = os.getpid()
                    self._queue = queue.Queue(maxsize=self._queue_size)
                    self._thread = None
        if self._thread is None or not self._thread.is_alive():
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._run, name="errorlens.transport", daemon=True)
                    self._thread.start()

    def _run(self) -> None:
        while True:
            body = self._queue.get()
            try:
                if body is None:
                    return
                self._send(body)
            except Exception:  # noqa: BLE001 - never let the worker die
                log.debug("errorlens: send failed", exc_info=True)
            finally:
                self._queue.task_done()

    def _send(self, body: bytes) -> None:
        if self._is_limited(""):
            return
        data = gzip.compress(body, compresslevel=6)
        if self._send_override is not None:
            status, headers = self._send_override(data)
        else:
            status, headers = self._post(data)
        if status == 429 or "retry-after" in headers:
            self._apply_retry_after(headers.get("retry-after"))
        if status >= 400:
            log.debug("errorlens: server answered %s", status)

    def _post(self, data: bytes) -> Tuple[int, Dict[str, str]]:
        req = urllib.request.Request(self.dsn.envelope_url, data=data, method="POST", headers={
            "Content-Type": "application/x-errorlens-envelope",
            "Content-Encoding": "gzip",
            "User-Agent": USER_AGENT,
            "X-ErrorLens-Key": self.dsn.public_key,
        })
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                resp.read()
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}
            finally:
                e.close()
        except (urllib.error.URLError, OSError) as e:
            log.debug("errorlens: could not reach %s: %s", self.dsn.host, e)
            return 0, {}

    # --- rate limits ----------------------------------------------------------------

    def _apply_retry_after(self, value: Optional[str]) -> None:
        try:
            seconds = float(value) if value else 60.0
        except ValueError:
            seconds = 60.0
        self._disabled_until[""] = time.monotonic() + max(1.0, min(seconds, 3600.0))

    def _is_limited(self, category: str) -> bool:
        now = time.monotonic()
        return self._disabled_until.get("", 0) > now or (bool(category) and self._disabled_until.get(category, 0) > now)

    # --- HTTP setup -----------------------------------------------------------------

    @staticmethod
    def _make_opener(proxy: Optional[str], ca_certs: Optional[str]) -> urllib.request.OpenerDirector:
        handlers: List[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if ca_certs:
            import ssl

            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_certs)))
        return urllib.request.build_opener(*handlers)
