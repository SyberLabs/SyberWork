"""Witness process. It holds the signing seed. The case writer does not.

The case writer verifies its own chain, then submits those events here.
This process checks the links again, signs the head, and appends the
envelope to the transparency log. The case writer keeps the public key
and opens that log read-only.

A process running as the same OS user can still open the log file
writable. The seed does not leave this process, and the case-writer API
cannot append. This is not Sigstore Rekor. Ed25519 here is not constant-time.
"""

from __future__ import annotations

import argparse
import json
import socket
import socketserver
import sys

from syberlabs.dsse import sign_chain_head, verify_chain_head
from syberlabs.ed25519 import generate
from syberlabs.errors import Rejected
from syberlabs.events import verify_events
from syberlabs.tlog import TransparencyLog


MAX_REQUEST = 8 * 1024 * 1024
_LOOPBACK = "127.0.0.1"


def _checked_case_id(events: object) -> str:
    if not isinstance(events, list) or not events or not all(isinstance(event, dict) for event in events):
        raise Rejected("invalid_chain", "witness requires the case events")
    case_id = events[0].get("case_id")
    if not isinstance(case_id, str) or not case_id or any(event.get("case_id") != case_id for event in events):
        raise Rejected("invalid_chain", "events must belong to one case")
    try:
        linked = verify_events(events)
    except (TypeError, KeyError, ValueError):
        linked = False
    if not linked:
        raise Rejected("invalid_chain", "case history does not verify")
    return case_id


class Witness:
    """In-process holder of the seed and the writable log. Used by the witness server."""

    def __init__(self, path: str, seed: bytes):
        self._seed = seed
        self._log = TransparencyLog(path, seed)
        self.public = self._log.public

    def attest(self, events: list) -> dict:
        """Check the chain, sign its head, and append the envelope. Returns the envelope."""
        case_id = _checked_case_id(events)
        envelope = sign_chain_head(case_id, events, self._seed)
        self._log.append(envelope)
        return envelope

    def close(self) -> None:
        self._log.close()


def _write_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > MAX_REQUEST:
        raise Rejected("witness_size", "witness message exceeds the request limit")
    sock.sendall(len(payload).to_bytes(4, "big") + payload)


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        piece = sock.recv(remaining)
        if not piece:
            raise Rejected("witness_unavailable", "witness closed the connection")
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def _read_frame(sock: socket.socket) -> bytes:
    size = int.from_bytes(_read_exact(sock, 4), "big")
    if size < 2 or size > MAX_REQUEST:
        raise Rejected("witness_size", "witness message exceeds the request limit")
    return _read_exact(sock, size)


def _error_frame(code: str, detail: str) -> bytes:
    if not code.isidentifier() or len(code) > 40:
        code = "witness_unavailable"
    return json.dumps({"error": code, "detail": detail[:200]}).encode()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            message = json.loads(_read_frame(self.request))
            events = message.get("events") if isinstance(message, dict) else None
            envelope = self.server.witness.attest(events)
            _write_frame(self.request, json.dumps({"envelope": envelope}).encode())
        except Rejected as exc:
            try:
                _write_frame(self.request, _error_frame(exc.code, exc.detail))
            except OSError:
                pass
        except (json.JSONDecodeError, UnicodeError, TimeoutError, OSError):
            try:
                _write_frame(self.request, _error_frame("witness_unavailable", "witness could not read the chain"))
            except OSError:
                pass


class WitnessServer(socketserver.ThreadingTCPServer):
    """Loopback server. The signing seed stays in this process."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, path: str, seed: bytes, port: int = 0):
        self.witness = Witness(path, seed)
        super().__init__((_LOOPBACK, port), _Handler)

    def handle_error(self, request, client_address) -> None:
        return None

    def close(self) -> None:
        self.server_close()
        self.witness.close()


class WitnessClient:
    """Case-writer side. It has the public key and a read-only log. It has no seed."""

    def __init__(self, port: int, public: bytes, log_path: str, *, host: str = _LOOPBACK):
        if host != _LOOPBACK:
            raise Rejected("invalid_target", "the case writer talks to a loopback witness only")
        if type(port) is not int or not 0 < port < 65536:
            raise Rejected("invalid_target", "witness port is not on loopback")
        self.host = host
        self.port = port
        self.public = public
        self._log = TransparencyLog.open_readonly(log_path, public)

    def submit(self, events: list) -> dict:
        """Ask the witness to sign this chain, then check the signature and the log."""
        payload = json.dumps({"events": events}).encode()
        if len(payload) > MAX_REQUEST:
            raise Rejected("witness_size", "case history exceeds the witness request limit")
        with socket.create_connection((self.host, self.port), timeout=30) as sock:
            sock.settimeout(30)
            _write_frame(sock, payload)
            raw = _read_frame(sock)
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            raise Rejected("witness_unavailable", "witness returned an unreadable response") from None
        if not isinstance(message, dict):
            raise Rejected("witness_unavailable", "witness returned an unreadable response")
        if message.get("error"):
            code = message["error"] if isinstance(message.get("error"), str) else "witness_unavailable"
            detail = message.get("detail") if isinstance(message.get("detail"), str) else "witness refused the chain"
            raise Rejected(code if code.isidentifier() and len(code) <= 40 else "witness_unavailable", detail[:200])
        envelope = message.get("envelope")
        if not isinstance(envelope, dict) or not verify_chain_head(envelope, events, self.public):
            raise Rejected("invalid_witness", "witness signature does not match this chain")
        if not self._log.verify_witnessed(envelope, events):
            raise Rejected("invalid_witness", "witness log does not include this chain head")
        return envelope

    def close(self) -> None:
        self._log.close()


def main(argv: list[str] | None = None) -> None:
    """Generate a seed in this process, print the public key and port, and serve."""
    parser = argparse.ArgumentParser(description="Hold the chain-head signing key and append to a transparency log.")
    parser.add_argument("--log", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    seed, public = generate()
    server = WitnessServer(args.log, seed, args.port)
    sys.stdout.write(json.dumps({"public": public.hex(), "port": server.server_address[1]}) + "\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.witness.close()


if __name__ == "__main__":
    main()
