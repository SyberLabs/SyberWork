"""``python -m syberlabs.factory``: the factory commands without the rest of the ``syberlabs`` CLI."""

import sys

from syberlabs.factory.cli import main

if __name__ == "__main__":
    sys.exit(main())
