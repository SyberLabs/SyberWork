"""A rehearsal of the GitHits A/B pilot: the whole pipeline, with a stand-in where the model would be.

Seven work items run through both arms of the factory's default proposer path (the repair loop) on a throwaway
repository: real Build Threads, real host checks, real acceptances, a real journal, then ``syberlabs metrics`` and
``syberlabs experiment report`` over it. Only the model is a stand-in, a command that answers the search protocol:

- on an item that depends on a library's behaviour (tag ``needs_lookup``) the stand-in guesses wrong once when GitHits
  is off, so the host's checks fail and the repair call fixes it; with GitHits on it gets it right first time;
- on a routine item it is right first time in both arms;
- on item ``LIB-4`` the treatment's GitHits server "fails to connect", as a misconfigured run would, so the report must
  refuse to count that item.

The stand-in declares the usage, lookups and connected MCP servers a real proposer reports (``docs/CLAUDE_CODE.md``):
those numbers are made up to exercise the report. **They are not results and say nothing about GitHits.** The pilot
itself runs a real model with a paid key (Class X: a named person runs it; ``docs/EXPERIMENTS.md`` has the commands).

    python examples/githits_ab.py [--out DIR]     # DIR receives report.json, report.md and the metrics CSV
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syberlabs import Kit, experiment, metrics  # noqa: E402
from syberlabs.build import default_contract  # noqa: E402
from syberlabs.providers import RepairProvider  # noqa: E402

NAME = "githits-rehearsal"
RUNNER = "runner@example.test"
PRICES = {"stand-in-model": {"input_per_million": 3, "output_per_million": 15}}  # a made-up price for a made-up model


def module(name: str, body: str, test: str) -> dict:
    return {f"src/{name}.py": textwrap.dedent(body),
            f"tests/test_{name}.py": "import os, sys, unittest\n"
                                     "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
                                     + textwrap.dedent(test)}


# Each item: (objective, tag, the right change, the stand-in's wrong first guess or None).
ITEMS = {
    "LIB-1": ("Parse ISO dates with datetime, keeping the UTC offset", "needs_lookup",
              module("dates", '''\
                  from datetime import datetime
                  def parse(text):
                      return datetime.fromisoformat(text)
                  ''', '''\
                  import dates
                  class Dates(unittest.TestCase):
                      def test_offset_kept(self):
                          self.assertEqual(dates.parse("2026-10-24T09:00:00+03:00").utcoffset().total_seconds(), 10800)
                  '''),
              module("dates", '''\
                  from datetime import datetime
                  def parse(text):
                      return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
                  ''', '''\
                  import dates
                  class Dates(unittest.TestCase):
                      def test_offset_kept(self):
                          self.assertEqual(dates.parse("2026-10-24T09:00:00+03:00").utcoffset().total_seconds(), 10800)
                  ''')),
    "LIB-2": ("Write CSV rows with csv.DictWriter, header first", "needs_lookup",
              module("rows", '''\
                  import csv, io
                  def to_csv(rows):
                      out = io.StringIO()
                      writer = csv.DictWriter(out, fieldnames=["name", "qty"], lineterminator="\\n")
                      writer.writeheader()
                      writer.writerows(rows)
                      return out.getvalue()
                  ''', '''\
                  import rows
                  class Rows(unittest.TestCase):
                      def test_header(self):
                          self.assertEqual(rows.to_csv([{"name": "a", "qty": 2}]), "name,qty\\na,2\\n")
                  '''),
              module("rows", '''\
                  import csv, io
                  def to_csv(rows):
                      out = io.StringIO()
                      writer = csv.DictWriter(out, fieldnames=["name", "qty"])
                      writer.writerows(rows)
                      return out.getvalue()
                  ''', '''\
                  import rows
                  class Rows(unittest.TestCase):
                      def test_header(self):
                          self.assertEqual(rows.to_csv([{"name": "a", "qty": 2}]), "name,qty\\na,2\\n")
                  ''')),
    "LIB-3": ("Round prices half-up to cents with decimal", "needs_lookup",
              module("money", '''\
                  from decimal import Decimal, ROUND_HALF_UP
                  def cents(value):
                      return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                  ''', '''\
                  import money
                  class Money(unittest.TestCase):
                      def test_half_up(self):
                          self.assertEqual(money.cents("2.675"), "2.68")
                  '''),
              module("money", '''\
                  def cents(value):
                      return f"{round(float(value), 2):.2f}"
                  ''', '''\
                  import money
                  class Money(unittest.TestCase):
                      def test_half_up(self):
                          self.assertEqual(money.cents("2.675"), "2.68")
                  ''')),
    "LIB-4": ("Join URL paths with urllib.parse.urljoin", "needs_lookup",
              module("links", '''\
                  from urllib.parse import urljoin
                  def link(base, path):
                      return urljoin(base if base.endswith("/") else base + "/", path)
                  ''', '''\
                  import links
                  class Links(unittest.TestCase):
                      def test_join(self):
                          self.assertEqual(links.link("https://x.test/docs", "a"), "https://x.test/docs/a")
                  '''),
              module("links", '''\
                  from urllib.parse import urljoin
                  def link(base, path):
                      return urljoin(base, path)
                  ''', '''\
                  import links
                  class Links(unittest.TestCase):
                      def test_join(self):
                          self.assertEqual(links.link("https://x.test/docs", "a"), "https://x.test/docs/a")
                  ''')),
    "ROUTINE-1": ("Add a function that sums quantities", "routine",
                  module("totals", '''\
                      def total(rows):
                          return sum(row["qty"] for row in rows)
                      ''', '''\
                      import totals
                      class Totals(unittest.TestCase):
                          def test_total(self):
                              self.assertEqual(totals.total([{"qty": 2}, {"qty": 3}]), 5)
                      '''), None),
    "ROUTINE-2": ("Add a function that title-cases names", "routine",
                  module("names", '''\
                      def title(name):
                          return name.title()
                      ''', '''\
                      import names
                      class Names(unittest.TestCase):
                          def test_title(self):
                              self.assertEqual(names.title("ada lovelace"), "Ada Lovelace")
                      '''), None),
    "ROUTINE-3": ("Add a function that drops empty rows", "routine",
                  module("clean", '''\
                      def clean(rows):
                          return [row for row in rows if row]
                      ''', '''\
                      import clean
                      class Clean(unittest.TestCase):
                          def test_clean(self):
                              self.assertEqual(clean.clean([{}, {"a": 1}]), [{"a": 1}])
                      '''), None),
}

# The stand-in "model". It reads the request on stdin and the arm from STAND_IN_ARM, and prints one candidate with
# the usage, lookups and MCP servers a real proposer would report. Every number in it is invented.
STAND_IN = textwrap.dedent('''\
    import json, os, sys
    items = json.loads(os.environ["STAND_IN_ITEMS"])
    request = json.load(sys.stdin)
    arm = os.environ["STAND_IN_ARM"]
    item = next(key for key, value in items.items() if value["objective"] == request["objective"])
    spec = items[item]
    lookup = spec["tag"] == "needs_lookup"
    broken = item == "LIB-4" and arm == "on"              # GitHits configured but not connected
    connected = arm == "on" and not broken
    repair = "feedback" in request
    wrong = lookup and spec["wrong"] is not None and not connected and not repair
    githits = (2 if lookup else 1) if connected and not repair else 0
    web = 3 if lookup and not connected and not repair else (1 if repair else 0)
    tokens = 1800 + 900 * githits + 600 * web + (1200 if repair else 0)
    servers = ["githits:" + ("connected" if connected else "failed")] if arm == "on" else []
    print(json.dumps({
        "candidates": [{"changes": spec["wrong"] if wrong else spec["right"], "message": item,
                        "signal": {"harness": {"name": "stand-in", "model": "stand-in-model", "mcp_servers": servers}}}],
        "recommended": [0],
        "usage": {"input_tokens": tokens, "output_tokens": 350, "model": "stand-in-model"},
        "tool_usage": {"githits": githits, "web_search": web, "web_fetch": 0}}))
    ''')


def git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def rehearse(folder: Path) -> tuple[list[dict], dict]:
    root = folder / "shop"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "__init__.py").write_text("")
    (root / "tests" / "test_smoke.py").write_text("import unittest\n\nclass Smoke(unittest.TestCase):\n"
                                                  "    def test_true(self):\n        self.assertTrue(True)\n")
    git(folder, "init", "-q", "-b", "main", str(root))
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")

    contract = default_contract(root)
    contract["evolution"]["budget"] = {"max_candidates": 4, "max_evaluations": 4, "max_seconds": 120, "prices": PRICES}
    home = root / ".syberlabs"
    (home / "contracts").mkdir(parents=True)
    (home / "contracts" / f"{contract['id']}.v{contract['version']}.json").write_text(json.dumps(contract))
    script = folder / "stand_in.py"
    script.write_text(STAND_IN)
    spec = {item: {"objective": objective, "tag": tag, "right": right, "wrong": wrong}
            for item, (objective, tag, right, wrong) in ITEMS.items()}

    kit = Kit.local(home, root, actor=RUNNER)
    try:
        for item, (objective, tag, _right, _wrong) in ITEMS.items():
            for arm in ("off", "on"):
                thread = kit.start(objective, f"{contract['id']}.v{contract['version']}", paths=["src/", "tests/"],
                                   unattended=True, actor=RUNNER)
                thread.record_experiment(NAME, item=item, arm=arm, by=RUNNER, tags=[tag])  # before any candidate
                os.environ.update({"STAND_IN_ARM": arm, "STAND_IN_ITEMS": json.dumps(spec)})
                found = thread.propose(RepairProvider([sys.executable, str(script)], name="stand-in", max_calls=3),
                                       seed=1)
                receipt = thread.accept(found[-1].id, actor=RUNNER) if found else None
                state = receipt.status if receipt else "no candidate"
                print(f"{item:10} {arm:3}  {len(found)} candidate(s), acceptance {state}")
        events = [e for case in kit.journal.thread_ids() for e in kit.session.history(case)]
        return events, dict(kit.session.contracts)
    finally:
        kit.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write report.json, report.md and metrics.csv here")
    args = parser.parse_args()
    print("SIMULATED: a stand-in answers instead of a model. These numbers check the pipeline; they are not results.\n")
    with tempfile.TemporaryDirectory() as folder:
        events, contracts = rehearse(Path(folder))
    items = metrics.work_items(events, contracts)
    found = experiment.report(events, NAME, contracts)
    print("\nPer work item (syberlabs metrics):")
    print(metrics.table(items))
    print("\nArms compared (syberlabs experiment report):")
    print(experiment.table(found))
    print("\nAs the write-up prints it (--markdown):")
    print(experiment.markdown(found))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
        (out / "report.md").write_text(experiment.markdown(found) + "\n", encoding="utf-8")
        (out / "metrics.csv").write_text(metrics.to_csv(items), encoding="utf-8")
        print(f"\nwrote {out / 'report.json'}, {out / 'report.md'} and {out / 'metrics.csv'}")
    excluded = found["checks"].get("arm_conflict", [])
    return 0 if excluded == ["LIB-4"] and found["items"]["compared"] == 6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
