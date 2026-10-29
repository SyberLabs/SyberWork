"""The factory: Space-Time Parsing of a repository on one ledger.

Space is the architecture graph derived from the source tree (``space``). Time
is a recorded execution (``harness``). Git is the commit axis. The ledger
(``ledger``) indexes cells keyed by component, step and commit; specs
(``spec``) claim footprints of cells, tickets name a delta over a footprint,
and attestation (``attest``) is a trace diff over that footprint, signed as a
DSSE envelope with the same Ed25519 code the chain head uses.

Standard library only. Nothing here imports ``syberwork``. v0 indexes Python
sources with ``ast``; other languages are an indexer away. The design and its
prior art are in ``docs/FACTORY.md``.
"""

from __future__ import annotations

__all__ = ["attest", "harness", "ledger", "space", "spec"]

FACTORY_VERSION = "0.1.0"
PROTOCOL = "syberlabs.space/factory/v0"
