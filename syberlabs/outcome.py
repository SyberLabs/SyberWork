"""The intervention effect: an admitted change is settled by whether a metric moved as predeclared.

The destination is a metric source, and it is only read. ``apply`` takes the baseline reading,
records it in the thread's hash chain at the sequence number after the claim, and then leaves the
claim open on purpose: nothing about the outcome can be known until the window has elapsed, so
``Session.commit`` stores ``effect_unknown`` with error ``window_open`` and ``publish`` returns
``unknown``. ``status`` reads the metric again once the window has elapsed, records that reading
too, and returns the kernel's own verdicts, so ``Session.reconcile`` settles the claim unchanged:
``applied`` when the metric moved by at least ``min_delta`` in the predeclared direction, and
``not_applied`` when it did not, a move against the expectation included. A reading that cannot
be taken, is unversioned, or is older than ``max_age_seconds`` leaves the claim pending, to be
retried on the next recover.

Attribution is not causation. ``applied`` records that the predeclared movement was observed in
the window after the change, and nothing more. This module imports nothing from ``publish`` or
``build`` at import time; ``publish`` imports it.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from syberlabs.clock import as_seconds, stamp
from syberlabs.errors import Rejected
from syberlabs.session import NoWrite
from syberlabs.targets import guard_request, trusted_origin

KEY = "intervention"                 # the observed-event key of both the baseline and the settlement reading
MAX_METRIC_BYTES = 65_536
MAX_SECONDS = 2_592_000              # 30 days: the longest window and the longest acceptable reading age
MAX_KEY_LENGTH = 200
DIRECTIONS = ("up", "down")
FIELDS = frozenset({"kind", "effect", "title", "metric_url", "metric_key", "window_seconds", "expectation",
                    "max_age_seconds", "auth_env", "timeout_seconds"})
# Refusals before the baseline is written. The source is only read and nothing is recorded yet,
# so each is a guaranteed no-write: 409 when the metric cannot be read as a number, 428 when the
# source gives no version to hold it by.
NO_WRITE = {"metric_unavailable": 409, "metric_shape": 409, "metric_too_large": 409, "missing_credential": 409,
            "source_unversioned": 428}


def _bad(detail: str) -> Rejected:
    return Rejected("invalid_action", detail)


def _seconds(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def validate(doc: dict) -> None:
    """Check an intervention action definition before it is installed. Every unknown field is refused."""
    from syberlabs.publish import _ENV   # the one environment-name pattern; lazy because publish imports this module
    if doc.get("kind") != "local" or doc.get("effect") != KEY:
        raise _bad("an intervention is kind local with effect intervention")
    url = doc.get("metric_url")
    if (not isinstance(url, str) or url.count("{") != 1 or url.count("}") != 1 or "?" in url or "#" in url
            or urlsplit(url).path.count("{key}") != 1):
        raise _bad("metric_url must contain exactly one {key} in its path and no other braces, query or fragment")
    try:
        trusted_origin(url.replace("{key}", "k"))
    except Rejected as exc:
        raise _bad(f"metric_url: {exc.detail}") from None
    key = doc.get("metric_key")
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_KEY_LENGTH or not key.isprintable() or "/" in key:
        raise _bad(f"metric_key must be 1 to {MAX_KEY_LENGTH} printable characters without a slash")
    if not _seconds(doc.get("window_seconds"), 1, MAX_SECONDS):
        raise _bad(f"window_seconds must be 1 to {MAX_SECONDS}")
    expectation = doc.get("expectation")
    if not isinstance(expectation, dict) or set(expectation) != {"direction", "min_delta"}:
        raise _bad("expectation must be {direction, min_delta} and nothing else")
    if expectation["direction"] not in DIRECTIONS:
        raise _bad("expectation.direction must be up or down")
    delta = expectation["min_delta"]
    if type(delta) not in (int, float) or not math.isfinite(delta) or delta <= 0:
        raise _bad("expectation.min_delta must be a finite number greater than zero")
    if "max_age_seconds" in doc and not _seconds(doc["max_age_seconds"], 1, MAX_SECONDS):
        raise _bad(f"max_age_seconds must be 1 to {MAX_SECONDS}")
    if "auth_env" in doc and (not isinstance(doc["auth_env"], str) or not _ENV.match(doc["auth_env"])):
        raise _bad("auth_env must name an environment variable")
    if "timeout_seconds" in doc and not _seconds(doc["timeout_seconds"], 1, 3600):
        raise _bad("timeout_seconds must be 1 to 3600")
    if "title" in doc and not isinstance(doc["title"], str):
        raise _bad("title must be a string")
    unknown = set(doc) - FIELDS
    if unknown:
        raise _bad(f"unknown field {sorted(unknown)[0]} for intervention")


@dataclass(frozen=True)
class Reading:
    """One reading: the value, the source's version of it, and when the source says it was observed."""

    value: float
    version: str
    observed_at: float | None


class MetricError(Exception):
    """The metric could not be read as a fresh, versioned number. ``code`` is all the record keeps."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Intervention:
    """Executor for the ``intervention`` effect. The metric source is read, never written."""

    def __init__(self, kit, doc: dict):
        from syberlabs.build import HOST   # build is imported whenever a Kit exists; lazy to avoid a cycle
        self.kit, self.doc, self.actor = kit, doc, HOST
        self.settle_seconds = doc["window_seconds"]   # the name Session.reconcile reads for its absent rule
        self.timeout = min(30, doc.get("timeout_seconds", 10))
        self.key = doc["metric_key"]
        self.url = doc["metric_url"].replace("{key}", quote(self.key, safe=""))
        self.expect = doc["expectation"]
        self.max_age = doc.get("max_age_seconds")

    # Reading

    def _now(self) -> float:
        """The session clock in seconds, through the exact expression Session.reconcile uses for the window."""
        return as_seconds(stamp(self.kit.session.clock()))

    def _read(self) -> Reading:
        from syberlabs.publish import HTTP   # the opener that refuses redirects
        headers = {"Accept": "application/json"}
        if self.doc.get("auth_env"):
            token = os.getenv(self.doc["auth_env"])
            if not token:
                raise MetricError("missing_credential")
            headers["Authorization"] = "Bearer " + token
        try:
            guard_request(self.url)
        except Rejected:
            raise MetricError("metric_unavailable") from None
        request = urllib.request.Request(self.url, headers=headers, method="GET")
        try:
            with HTTP.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_METRIC_BYTES + 1)
                version = response.headers.get("ETag") or response.headers.get("X-Record-Version")
        except (OSError, http.client.HTTPException):   # HTTPError, URLError, timeouts and resets are all OSError
            raise MetricError("metric_unavailable") from None
        if len(raw) > MAX_METRIC_BYTES:
            raise MetricError("metric_too_large")
        try:
            document = json.loads(raw)
        except ValueError:
            raise MetricError("metric_shape") from None
        value = document.get("value") if isinstance(document, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value):
            raise MetricError("metric_shape")
        if not version:
            raise MetricError("source_unversioned")
        observed_at = document.get("observed_at")
        if type(observed_at) not in (int, float) or not math.isfinite(observed_at):
            observed_at = None
        return Reading(float(value), str(version), None if observed_at is None else float(observed_at))

    def _fresh(self, reading: Reading, now: float) -> bool:
        return self.max_age is None or (reading.observed_at is not None and now - reading.observed_at <= self.max_age)

    # The claim

    def apply(self, case_id: str, args: dict, key: str) -> dict:
        """Read the baseline, record it at the sequence after the claim, and leave the claim open."""
        now = self._now()
        try:
            reading = self._read()
        except MetricError as exc:
            # Nothing was written: the destination is only read, and no baseline is recorded yet.
            raise NoWrite(NO_WRITE[exc.code], exc.code) from None
        if not self._fresh(reading, now):
            raise NoWrite(428, "baseline_stale")
        self.kit.session.observe(case_id, KEY, {
            "proposal_id": key, "phase": "baseline", "metric": self.key, "commit": args["commit"],
            "value": reading.value, "version": reading.version, "observed_at": reading.observed_at, "read_at": now,
        }, "metric:" + self.key, reading.version, self.actor, verified=True)
        # The outcome cannot be known now. Session.commit records effect_unknown with error window_open.
        raise Rejected("window_open", f"the metric is read again after {self.settle_seconds} seconds; run syberlabs recover")

    # The settlement

    def _claim_and_baseline(self, case_id: str, key: str) -> tuple[dict | None, dict | None]:
        """The claim for this proposal, and the baseline the host recorded right after it, or None for each.

        Only the event at ``claim.seq + 1`` is read. The baseline is written inside ``commit`` under
        the session lock as the next append after the claim, so nothing else can hold that position;
        an observation appended later with the same shape is not a baseline.
        """
        events = self.kit.session.history(case_id)
        claim = next((e for e in events if e["kind"] == "effect_started" and e["body"]["proposal_id"] == key), None)
        if claim is None:
            return None, None
        after = next((e for e in events if e["seq"] == claim["seq"] + 1), None)
        body = after["body"] if after is not None and after["kind"] == "observed" else {}
        value = body.get("value") if isinstance(body.get("value"), dict) else {}
        ok = (body.get("key") == KEY and body.get("verified") is True and body.get("actor") == self.actor
              and value.get("proposal_id") == key and value.get("phase") == "baseline"
              and type(value.get("value")) in (int, float) and math.isfinite(value["value"])
              and isinstance(value.get("version"), str))
        return claim, (value if ok else None)

    def status(self, case_id: str, args: dict, key: str) -> tuple[str, dict]:
        """The kernel's verdict for an open claim. Writes the settlement reading only with a terminal verdict."""
        claim, baseline = self._claim_and_baseline(case_id, key)
        if claim is None:
            return "unknown", {"error": "claim_missing"}
        now = self._now()
        elapsed = now - as_seconds(claim["at"])
        if baseline is None:
            # Nothing to measure against. Session.reconcile voids the claim once the window has passed.
            return "absent", {"reason": "baseline_missing"}
        if elapsed < self.settle_seconds:
            return "absent", {"reason": "window_open", "remaining_seconds": int(self.settle_seconds - elapsed)}
        try:
            reading = self._read()
        except MetricError as exc:
            return "unknown", {"error": exc.code}
        if not self._fresh(reading, now):
            return "unknown", {"error": "metric_stale"}
        delta = reading.value - baseline["value"]
        gain = delta if self.expect["direction"] == "up" else -delta
        outcome = "moved" if gain >= self.expect["min_delta"] else "moved_against" if gain < 0 else "unchanged"
        self.kit.session.observe(case_id, KEY, {
            "proposal_id": key, "phase": "settlement", "metric": self.key, "commit": args["commit"],
            "baseline": baseline["value"], "baseline_version": baseline["version"],
            "value": reading.value, "version": reading.version, "observed_at": reading.observed_at, "read_at": now,
            "delta": delta, "direction": self.expect["direction"], "min_delta": self.expect["min_delta"],
            "window_seconds": self.settle_seconds, "outcome": outcome,
        }, "metric:" + self.key, reading.version, self.actor, verified=True)
        proof = {"external_id": reading.version, "outcome": outcome, "baseline": baseline["value"],
                 "baseline_version": baseline["version"], "value": reading.value, "delta": delta}
        return ("applied" if outcome == "moved" else "not_applied"), proof
