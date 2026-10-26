"""Patterns shared by modules that must not import each other.

Standard library only and nothing from ``syberlabs``: the query guard's hook process loads this file by path, beside
itself, so a lookup does not pay for importing the whole package (``syberlabs.query_guard``).
"""

import re

# Shapes of common credentials. ``metrics`` refuses a note that looks like a key; the query guard blocks a lookup.
SECRET = re.compile(r"(ghp_|gho_|ghs_|github_pat_|sk-ant-|sk-|xox[abprs]-|AKIA)[A-Za-z0-9_\-]{8,}")
