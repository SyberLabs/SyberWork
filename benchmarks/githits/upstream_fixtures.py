"""Pinned upstream modules and issue-derived host checks; no model pilots."""

import json
from pathlib import Path

DATA = Path(__file__).with_name("upstream-v3")
UPSTREAM_FIXTURES = {path.stem: json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted(DATA.glob("U*.json"))}
