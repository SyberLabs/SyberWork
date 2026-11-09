"""One loopback origin that plays source, destination, planner, and settlement."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from syberlabs.canonical import digest


class Origin(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.orders: dict[str, dict] = {}
        self.dropped: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.secret_hits = 0
        self.redirect_hits = 0
        self.planner_hits = 0
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(2)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _path(self) -> str:
        return unquote(urlsplit(self.path).path)

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload, etag: str | None = None) -> None:
        headers = {"Content-Type": "application/json"}
        if etag is not None:
            headers["ETag"] = etag
        self._send(status, json.dumps(payload).encode(), headers)

    def do_GET(self):
        path = self._path()
        origin: Origin = self.server  # type: ignore[assignment]
        if path.startswith("/inventory/"):
            key = path.removeprefix("/inventory/")
            self._json(200, {"part_number": key}, "inv:7")
            return
        if path.startswith("/quotes/"):
            key = path.removeprefix("/quotes/")
            self._json(200, {"quote": {"id": key, "price": 250}}, "quote:1")
            return
        if path.startswith("/orders/by-key/"):
            key = path.removeprefix("/orders/by-key/")
            self._status(origin.orders.get(key))
            return
        if path.startswith("/drop/by-key/"):
            key = path.removeprefix("/drop/by-key/")
            self._status(origin.dropped.get(key))
            return
        if path.startswith("/status/"):
            key = path.removeprefix("/status/")
            record = origin.receipts.get(key)
            if record is None:
                self._send(404, b"")
                return
            self._json(200, record)
            return
        if path.startswith("/redirect/"):
            origin.redirect_hits += 1
            self._send(302, b"", {"Location": origin.base + "/secret/" + path.removeprefix("/redirect/")})
            return
        if path.startswith("/secret/"):
            origin.secret_hits += 1
            self._json(200, {"part_number": "stolen"})
            return
        if path in ("/languages", "/v1/languages"):
            # GitHits doctor asks {base}/languages (the client strips the /v1 prefix).
            self._json(200, ["python"])
            return
        self._send(404, b"")

    def _status(self, record: dict | None) -> None:
        if record is None:
            self._send(404, b"")
            return
        self._json(200, record)

    def do_POST(self):
        origin: Origin = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        path = self._path()
        if path == "/suggest":
            origin.planner_hits += 1
            self._json(200, {"action": "record_review", "args": {"part_number": "P-104"}})
            return
        if path == "/orders":
            key = self.headers["Idempotency-Key"]
            args = json.loads(raw)
            origin.orders[key] = {
                "state": "committed",
                "idempotency_key": key,
                "request_digest": digest(args),
                "external_id": "ord-" + key[:8],
            }
            self._send(201, b"")
            return
        if path == "/settle":
            key = self.headers["Idempotency-Key"]
            args = json.loads(raw)
            origin.receipts[key] = {
                "state": "settled",
                "idempotency_key": key,
                "request_digest": digest(args),
                "amount_units": args["amount_units"],
                "asset": args["asset"],
                "counterparty": args["counterparty"],
                "external_id": "tx-" + key[:8],
            }
            self._json(200, origin.receipts[key])
            return
        if path == "/drop":
            key = self.headers["Idempotency-Key"]
            args = json.loads(raw)
            origin.dropped[key] = {
                "state": "committed",
                "idempotency_key": key,
                "request_digest": digest(args),
                "external_id": "drop-" + key[:8],
            }
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        self._send(404, b"")
