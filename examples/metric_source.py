"""A loopback metric source for the ``intervention`` effect: one number per key, versioned by an ETag.

Run with: python examples/metric_source.py --port 8998
Then point an intervention at http://127.0.0.1:8998/metrics/{key}. Replace this with your own
metric system; the contract it must meet is in docs/OUTCOMES.md.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit


class MetricSource:
    """One number per key. Every write bumps the key's version, which GET returns as the ETag.

    Switches, for tests: ``unversioned`` omits the ETag header; ``fail`` answers every GET with
    500 without closing the port; ``token``, when set, makes GET require ``Authorization:
    Bearer <token>``. ``url`` is a template with the literal ``{key}`` an intervention needs.
    """

    def __init__(self, port: int = 0, values: dict | None = None):
        self._lock = threading.Lock()
        self._values: dict[str, tuple[object, float | None]] = {}
        self._versions: dict[str, int] = {}
        self.unversioned = False
        self.fail = False
        self.token: str | None = None
        for key, value in (values or {}).items():
            self.set(key, value)
        source = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send_json(self, body, status=200, etag=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if etag:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(data)

            def key(self):
                path = urlsplit(self.path).path
                if path.startswith("/metrics/") and path.count("/") == 2:
                    return unquote(path.split("/", 2)[2])
                return None

            def do_GET(self):
                key = self.key()
                if key is None:
                    return self.send_json({"error": "not_found"}, 404)
                if source.fail:
                    return self.send_json({"error": "unavailable"}, 500)
                if source.token and self.headers.get("Authorization") != "Bearer " + source.token:
                    return self.send_json({"error": "unauthorized"}, 401)
                with source._lock:
                    if key not in source._values:
                        return self.send_json({"error": "unknown_metric"}, 404)
                    value, observed_at = source._values[key]
                    version = source._version(key)
                self.send_json({"key": key, "value": value, "observed_at": observed_at},
                               etag=None if source.unversioned else version)

            def do_POST(self):
                key = self.key()
                if key is None:
                    return self.send_json({"error": "not_found"}, 404)
                try:
                    data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                    value, observed_at = data["value"], data.get("observed_at")
                except (ValueError, KeyError, TypeError):
                    return self.send_json({"error": "invalid_body"}, 400)
                if type(value) not in (int, float) or type(observed_at) not in (int, float, type(None)):
                    return self.send_json({"error": "invalid_body"}, 400)
                self.send_json({"key": key, "version": source.set(key, value, observed_at)})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}/metrics/{{key}}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def _version(self, key: str) -> str:
        return f"{key}-v{self._versions[key]}"

    def set(self, key: str, value, observed_at: float | None = None) -> str:
        """Store a value (a number; tests also store other JSON to exercise shape refusals). Returns the new ETag."""
        with self._lock:
            self._values[key] = (value, observed_at)
            self._versions[key] = self._versions.get(key, 0) + 1
            return self._version(key)

    def get(self, key: str) -> tuple[object, str, float | None]:
        with self._lock:
            value, observed_at = self._values[key]
            return value, self._version(key), observed_at

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8998)
    options = parser.parse_args()
    source = MetricSource(options.port)
    source.set("checkout_conversion_7d", 2.0, observed_at=time.time())
    print("Metric source listening at " + source.url
          + '; POST the same path with {"value": <number>, "observed_at": <epoch seconds or null>} to move it', flush=True)
    try:
        source._thread.join()
    except KeyboardInterrupt:
        source.close()
