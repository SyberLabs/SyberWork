"""Patterns shared by modules that must not import each other.

Standard library only and nothing from ``syberlabs``: the query guard's hook process loads this file by path, beside
itself, so a lookup does not pay for importing the whole package (``syberlabs.query_guard``).
"""

import re

# Shapes of common credentials. ``metrics`` refuses a note that looks like a key; the query guard blocks a lookup.
SECRET = re.compile(r"(ghp_|gho_|ghs_|github_pat_|sk-ant-|sk-|xox[abprs]-|AKIA)[A-Za-z0-9_\-]{8,}")


def credential_prefixes(text: str) -> list[str]:
    """The ``SECRET`` prefixes in ``text`` that head a credential, never the matched key itself.

    A prefix counts wherever it is not glued to a preceding letter or digit: at the start, after whitespace or
    punctuation, and after ``_`` (``key_sk-ant-api03-...`` holds a key). A rejected start never hides a later prefix in
    the same token: in ``task-ghp_...`` the ``sk-`` inside ``task-`` follows ``a`` and does not count, and the search
    resumes one character on, so the ``ghp_`` after the hyphen does. Ordinary words ending in ``sk`` followed by a
    hyphen (``risk-assessment``, ``task-list``, ``disk-cleanup``) are therefore not ``sk-`` keys.
    """
    found, position = [], 0
    while True:
        match = SECRET.search(text, position)
        if match is None:
            return found
        if match.start() == 0 or not text[match.start() - 1].isalnum():
            found.append(match.group(1))
            position = match.end()
        else:
            position = match.start() + 1
