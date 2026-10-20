"""SyberLabs SDK core. Standard library only; no dependency on syberwork."""

from syberlabs.admission import explain
from syberlabs.canonical import canonical, digest
from syberlabs.dsse import sign_chain_head, verify_chain_head
from syberlabs.ed25519 import generate as generate_signing_key
from syberlabs.errors import Rejected
from syberlabs.events import event_digest, verify_events
from syberlabs.interop import at_microseconds, cloudevent, witness
from syberlabs.jcs import jcs_bytes, jcs_digest
from syberlabs.mappings import translate_stabilize
from syberlabs.search import Candidate, SearchProvider
from syberlabs.session import Session
from syberlabs.targets import trusted_origin
from syberlabs.tlog import TransparencyLog
from syberlabs.witness import Witness, WitnessClient

__all__ = [
    "CERTIFICATE_TYPE",
    "Candidate",
    "Kit",
    "Receipt",
    "Rejected",
    "SearchProvider",
    "Session",
    "Thread",
    "TransparencyLog",
    "Verdict",
    "Witness",
    "WitnessClient",
    "at_microseconds",
    "canonical",
    "certify",
    "cloudevent",
    "digest",
    "event_digest",
    "explain",
    "generate_signing_key",
    "jcs_bytes",
    "jcs_digest",
    "sign_chain_head",
    "translate_stabilize",
    "trusted_origin",
    "verify_certificate",
    "verify_chain_head",
    "verify_events",
    "witness",
]

# Names that load on first use, so ``import syberlabs`` stays small: the Build Thread and the certificate.
_LAZY = {
    "Kit": "build", "Thread": "build", "Verdict": "build", "Receipt": "build",
    "CERTIFICATE_TYPE": "certificate", "certify": "certificate", "verify_certificate": "certificate",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'syberlabs' has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(f"syberlabs.{module}"), name)
