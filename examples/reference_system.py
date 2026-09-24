"""A separate, persistent source system for the included purchase-order workflow.

Run with: python examples/reference_system.py --database .reference/system.sqlite3
Replace this adapter and the registered source/action URLs with your own systems.
"""

import argparse
import hashlib
import json
import sqlite3
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def run(database: Path, port: int):
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (part_number TEXT PRIMARY KEY, version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, part_number TEXT NOT NULL, price REAL NOT NULL, supplier TEXT NOT NULL, version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, part_number TEXT NOT NULL, quote_id TEXT NOT NULL, amount REAL NOT NULL, request_digest TEXT);
            INSERT OR IGNORE INTO inventory VALUES ('P-104', 'inventory-v1');
            INSERT OR IGNORE INTO quotes VALUES ('Q-7', 'P-104', 250, 'Example Supply', 'quote-v1');
        """)
        if "request_digest" not in {row[1] for row in db.execute("PRAGMA table_info(orders)")}:
            db.execute("ALTER TABLE orders ADD COLUMN request_digest TEXT")

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, body, status=200, etag=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            with sqlite3.connect(database) as db:
                if path.startswith("/inventory/"):
                    key = unquote(path.split("/", 2)[2])
                    record = db.execute("SELECT part_number,version FROM inventory WHERE part_number=?", (key,)).fetchone()
                    return self.send_json({"part_number": record[0]}, etag=record[1]) if record else self.send_json({"error": "unknown_part"}, 404)
                if path.startswith("/quotes/"):
                    key = unquote(path.split("/", 2)[2])
                    record = db.execute("SELECT part_number,price,supplier,version FROM quotes WHERE id=?", (key,)).fetchone()
                    return self.send_json({"quote": {"id": key, "part_number": record[0], "price": record[1], "supplier": record[2]}}, etag=record[3]) if record else self.send_json({"error": "unknown_quote"}, 404)
                if path.startswith("/orders/"):
                    if path.startswith("/orders/by-key/"):
                        key = unquote(path.split("/", 3)[3])
                        record = db.execute("SELECT id,request_digest FROM orders WHERE idempotency_key=?", (key,)).fetchone()
                        return self.send_json({"state": "committed", "external_id": record[0], "idempotency_key": key, "request_digest": record[1]}) if record else self.send_json({"error": "not_found"}, 404)
                    record = db.execute("SELECT id,part_number,quote_id,amount FROM orders WHERE id=?", (unquote(path.split("/", 2)[2]),)).fetchone()
                    return self.send_json({"id": record[0], "part_number": record[1], "quote_id": record[2], "amount": record[3]}) if record else self.send_json({"error": "unknown_order"}, 404)
            self.send_json({"error": "not_found"}, 404)

        def do_POST(self):
            if urlsplit(self.path).path != "/orders":
                return self.send_json({"error": "not_found"}, 404)
            key = self.headers.get("Idempotency-Key")
            version = self.headers.get("If-Match")
            if not key or not version:
                return self.send_json({"error": "precondition_required"}, 428)
            try:
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            except (ValueError, KeyError):
                return self.send_json({"error": "invalid_body"}, 400)
            with sqlite3.connect(database, timeout=10) as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT id FROM orders WHERE idempotency_key=?", (key,)).fetchone()
                if old:
                    return self.send_json({"id": old[0], "replayed": True})
                quote = db.execute("SELECT id,part_number,price,version FROM quotes WHERE id=?", (data.get("quote_id"),)).fetchone()
                if not quote or quote[3] != version or quote[1] != data.get("part_number") or quote[2] != data.get("amount") or data.get("quote", {}).get("price") != quote[2]:
                    return self.send_json({"error": "quote_changed_or_mismatch"}, 412)
                order_id = str(uuid.uuid4())
                fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (order_id, key, data["part_number"], quote[0], data["amount"], fingerprint))
            self.send_json({"id": order_id, "replayed": False}, 201)

        def log_message(self, format, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Reference source system listening at http://127.0.0.1:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path(".reference/system.sqlite3"))
    parser.add_argument("--port", type=int, default=8999)
    options = parser.parse_args()
    run(options.database, options.port)
