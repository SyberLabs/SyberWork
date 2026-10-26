"""scripts/pilot_dry_run.py, offline: the GitHits pilot's dry run as one command, with no model and no network.

``claude`` is the stand-in of ``tests/test_claude_code.py`` (``ClaudeCodeCase.stand_in``): it runs the real query guard
hook from the ``--settings`` file, emulates Claude Code's Bash tool (the launcher runs, any other command is denied
under ``dontAsk``), writes a change and prints a recorded stream. GitHits is the loopback ``FakeGitHits`` of
``tests/test_githits.py``. Every call the stand-in makes asks for the two lookups the canary's objective asks for:
one the guard must refuse (it holds an email address) and one it passes, so the treatment's launcher sends one
search to the fake and the baseline's Bash calls are denied. Most tests run the ``syberlabs`` commands in this
process (``in_process``); ``OneCommand`` runs the script as the operator does, every command a subprocess.
"""

import contextlib
import importlib.util
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from syberlabs import claude_code, experiment, githits, metrics, query_guard
from syberlabs.cli import main as cli
from tests.test_build_thread import git
from tests.test_claude_code import BROKEN, COST, ClaudeCodeCase, launcher_lookup
from tests.test_claude_code import PRICES as INPUT_OUTPUT
from tests.test_githits import FakeGitHits

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pilot_dry_run.py"
SPEC = importlib.util.spec_from_file_location("pilot_dry_run", SCRIPT)
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)

SEED = 7
# The stand-in's model at all four rates the plan must give, its cache rates the input rate: so the host's arithmetic
# is test_claude_code's COST.
PRICES = {model: {**rates, "cache_read_per_million": rates["input_per_million"],
                  "cache_creation_per_million": rates["input_per_million"]} for model, rates in INPUT_OUTPUT.items()}
# What the canary asks the model to run, and what the stand-in runs on every call.
LOOKUPS = [launcher_lookup(pilot.CANARY_BLOCKED), launcher_lookup(pilot.CANARY_PASSING)]
# Variables a test must not inherit from the shell (a Claude Code cloud session sets two of the switches).
FOREIGN = (*claude_code.THIRD_PARTY_SWITCHES, pilot.SCRUB_ENV, claude_code.BIN_ENV, githits.EXCLUDE_ENV,
           githits.EXCLUDE_REPLACE_ENV, githits.LICENSES_ENV, githits.SESSION_ENV, githits.AGENT_ENV,
           githits.RECORD_DIR_ENV, githits.RECORD_KEY_ENV, query_guard.LINES_ENV, query_guard.CHARS_ENV,
           "SYBERLABS_CERTIFICATE_SEED", "SYBERLABS_CHECK_ENV")


def in_process(argv, env, timeout):
    """The ``syberlabs`` CLI in this process: the same ``main`` the subprocess runs, without the start-up time."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli(list(argv))
        except SystemExit as exc:  # an argparse usage error
            code = exc.code if isinstance(exc.code, int) else 2
    return pilot.Done(code, out.getvalue(), err.getvalue())


def answers(*replies):
    """An ``ask`` that gives ``replies`` in turn, then the end of input."""
    found = iter(replies)

    def ask(prompt):
        try:
            return next(found)
        except StopIteration:
            raise EOFError from None
    return ask


class DryRunCase(ClaudeCodeCase):
    """A repository with a passing test suite (``RepoCase``), a fake GitHits, the operator's credentials as
    placeholders, and nothing else from the shell that could route or scrub a call."""

    def setUp(self):
        super().setUp()
        self.fake = FakeGitHits()
        self.addCleanup(self.fake.close)
        self.token = "ghtest_" + secrets.token_hex(16)
        self.api_key = "sk-ant-placeholder-" + secrets.token_hex(12)
        patcher = mock.patch.dict(os.environ, {githits.TOKEN_ENV: self.token, githits.URL_ENV: self.fake.url,
                                               pilot.API_KEY_ENV: self.api_key})
        patcher.start()
        self.addCleanup(patcher.stop)  # restores every variable popped below too
        for name in FOREIGN:
            os.environ.pop(name, None)
        self.claude("claude")

    def claude(self, name, **options):
        """Install a stand-in ``claude`` (``SYBERLABS_CLAUDE_BIN``) that makes ``LOOKUPS`` and writes ``GOOD``."""
        options.setdefault("lookups", LOOKUPS)
        self.bin = name
        os.environ[claude_code.BIN_ENV] = " ".join(self.stand_in(name, **options))

    def plan(self, items=3, name="plan", **extra) -> Path:
        found = [{"id": f"LIB-{n}", "tags": ["needs_lookup"] if n < items else ["routine"],
                  "objective": f"Add a CSV export for report rows, variant {n}", "paths": ["src", "tests"]}
                 for n in range(1, items + 1)]
        doc = {"experiment": "githits-dry-run", "items": found, "prices": PRICES, "max_calls": 2, "max_turns": 8,
               **extra}
        path = self.root.parent / f"{name}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def dry_run(self, *flags, plan=None, out="run", ask=None, runner=in_process):
        """Run the script's ``main`` here; returns (exit code, what it printed, the receipt)."""
        self.out = (self.root.parent / out).resolve()
        printed = io.StringIO()
        argv = ["--plan", str(plan or self.plan()), "--repo", str(self.root), "--out", str(self.out), "--seed",
                str(SEED), "--yes", *flags]
        with contextlib.redirect_stdout(printed):
            code = pilot.main(argv, ask=ask or answers(*["pass"] * 12), runner=runner)
        receipt = json.loads((self.out / "receipt.json").read_text(encoding="utf-8"))
        return code, printed.getvalue(), receipt

    def check(self, receipt, name) -> dict:
        return next(c for c in receipt["preflight"] if c["name"] == name)


class Schedule(unittest.TestCase):
    def test_both_arms_of_an_item_run_back_to_back_in_a_seeded_balanced_order(self):
        items = ["LIB-1", "LIB-2", "LIB-3", "RT-1", "RT-2"]
        order = pilot.schedule(items, 11)
        self.assertEqual(order, pilot.schedule(items, 11), "the same seed, the same order")
        self.assertEqual(len(order), 10)
        pairs = [order[i:i + 2] for i in range(0, len(order), 2)]
        for (item, first), (again, second) in pairs:
            self.assertEqual(item, again, "an item's two arms are adjacent")
            self.assertEqual({first, second}, {"off", "on"})
        self.assertEqual(sorted(item for (item, _), _ in pairs), sorted(items), "every item once")
        firsts = [first for (_, first), _ in pairs]
        self.assertLessEqual(abs(firsts.count("off") - firsts.count("on")), 1, "first arms balanced")
        orders = {tuple(pilot.schedule(items, seed)) for seed in range(40)}
        self.assertGreater(len(orders), 20, "the seed sets the order")
        self.assertGreater(len({order[0] for order in orders}), 5, "and which item and arm run first")

    def test_the_nine_are_the_metrics_suites_nine(self):
        self.assertEqual(pilot.NINE, metrics.METRICS)
        self.assertEqual(set(pilot.PAIRED.values()) - set(experiment.COMPARED), {"reviewed"},
                         "time_to_pass is compared; reviewed is the delivered outcome under --delivered review")


class Plan(unittest.TestCase):
    def valid(self, **changes):
        doc = {"items": [{"id": f"A-{n}", "tags": ["needs_lookup"], "objective": f"change {n}", "paths": ["src"]}
                         for n in range(3)], "prices": PRICES}
        doc.update(changes)
        return doc

    def test_a_plan_is_three_to_five_named_items_with_prices(self):
        plan = pilot.validate_plan(self.valid(_comment="ignored"))
        self.assertEqual((plan["experiment"], plan["max_calls"], plan["max_turns"]),
                         (pilot.DEFAULT_EXPERIMENT, pilot.DEFAULT_MAX_CALLS, pilot.DEFAULT_MAX_TURNS))
        self.assertEqual(plan["allowed_tools"], "Read,Edit,Write,Glob,Grep,WebSearch,WebFetch", "the pilot's")
        self.assertEqual(experiment.parse_items(pilot.items_text(plan["items"], plan["experiment"])),
                         {"A-0": ("needs_lookup",), "A-1": ("needs_lookup",), "A-2": ("needs_lookup",)})
        refused = {
            "two items": self.valid(items=self.valid()["items"][:2]),
            "six items": self.valid(items=[{"id": f"B-{n}", "objective": f"x {n}"} for n in range(6)]),
            "same id": self.valid(items=[{"id": "A", "objective": f"x {n}"} for n in range(3)]),
            "same objective": self.valid(items=[{"id": f"A-{n}", "objective": "x"} for n in range(3)]),
            "an unknown key": self.valid(budget=3),
            "no prices": {k: v for k, v in self.valid().items() if k != "prices"},
            "the template's prices": self.valid(prices=pilot.TEMPLATE["prices"]),
            "no cache rates": self.valid(prices=INPUT_OUTPUT),
            "a free cache": self.valid(prices={m: {**r, "cache_read_per_million": 0} for m, r in PRICES.items()}),
            "a shell": self.valid(allowed_tools="Read,Edit,Bash"),
            "prices and a contract": self.valid(contract={"evolution": {}}),
            "a path out of the tree": self.valid(items=[{"id": f"A-{n}", "objective": f"x {n}", "paths": ["../x"]}
                                                        for n in range(3)]),
            "a Windows path out of the tree": self.valid(items=[{"id": f"A-{n}", "objective": f"x {n}",
                                                                 "paths": ["src\\..\\..\\x"]} for n in range(3)]),
            "a canary path out of the tree": self.valid(canary_paths=["..\\x"]),
            "an objective like a flag": self.valid(items=[{"id": f"A-{n}", "objective": f"--x {n}"} for n in range(3)]),
        }
        for why, doc in refused.items():
            with self.subTest(why), self.assertRaises(pilot.PlanError):
                pilot.validate_plan(doc)
        with self.assertRaisesRegex(pilot.PlanError, "cache_read_per_million, cache_creation_per_million .*cache"):
            pilot.validate_plan(self.valid(prices=INPUT_OUTPUT))  # says which rates, and why they matter


class Arguments(unittest.TestCase):
    def run_main(self, *argv):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = pilot.main(list(argv), ask=answers(), runner=in_process)
        return code, printed.getvalue()

    def test_the_template_and_the_arguments_refused_before_anything_is_written(self):
        code, printed = self.run_main("--template")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed)["items"][0]["id"], "LIB-1")
        with self.assertRaises(pilot.PlanError, msg="the template must be filled in"):
            pilot.validate_plan(json.loads(printed))
        with tempfile.TemporaryDirectory() as folder:
            busy = Path(folder) / "busy"
            busy.mkdir()
            (busy / "x").write_text("x", encoding="utf-8")
            for argv, words in ((["--repo", folder], "--plan is required"),
                                (["--plan", "p.json", "--reviewer", "two words"], "--reviewer must be a short name"),
                                (["--plan", "p.json", "--repo", str(Path(folder) / "nowhere")], "is not a directory"),
                                (["--plan", "p.json", "--repo", folder, "--out", str(busy)], "is not empty")):
                with self.subTest(words):
                    code, printed = self.run_main(*argv)
                    self.assertEqual(code, 2)
                    self.assertIn(words, printed)
            self.assertFalse((Path(folder) / "nowhere").exists(), "nothing is created for a refused argument")
            self.assertEqual(sorted(p.name for p in busy.iterdir()), ["x"])


class Pieces(unittest.TestCase):
    """The budget, the contracts, the gate and the report's exclusions, one by one."""

    def plan(self, **changes):
        doc = {"items": [{"id": f"A-{n}", "objective": f"change {n}"} for n in range(3)], "prices": PRICES}
        return pilot.validate_plan({**doc, **changes})

    def test_the_derived_contracts_price_every_call_and_hold_each_thread_to_its_share(self):
        with mock.patch("sys.executable", "/opt/python"):
            main, canary = pilot.build_contracts(self.plan(checks={"lint": {"argv": ["/opt/lint"]}}), ROOT, 1.25)
        self.assertEqual(main["evolution"]["budget"], {"max_candidates": 12, "max_evaluations": 24,
                                                       "max_seconds": 3600, "prices": PRICES, "max_dollars": 1.25,
                                                       "strict": True})
        self.assertEqual(main["evolution"]["evaluation"]["checks"], {"lint": {"argv": ["/opt/lint"]}})
        self.assertEqual(main["evolution"]["evaluation"]["required"], ["lint"])
        self.assertEqual((canary["id"], canary["version"], canary["evolution"]["evaluation"]["required"]),
                         ("pilot-canary", 1, ["canary"]))
        self.assertEqual(canary["evolution"]["evaluation"]["checks"]["canary"]["argv"], ["/opt/python", "-c", "pass"])
        self.assertEqual((canary["evolution"]["budget"]["prices"], canary["evolution"]["budget"]["max_dollars"]),
                         (PRICES, 1.25), "the canary is priced and capped as the arms are")
        self.assertEqual(pilot.check_budget(8.0, main, 6).status, "PASS")
        own, _ = pilot.build_contracts(self.plan(prices=None, contract=main), ROOT, 0.5)
        self.assertEqual(own, main, "a whole contract in the plan is used as it is")

    def test_the_budget_check_wants_prices_a_share_under_the_cap_and_strict(self):
        main, _ = pilot.build_contracts(self.plan(), ROOT, 1.0)

        def without(key):
            doc = json.loads(json.dumps(main))
            del doc["evolution"]["budget"][key]
            return doc
        def with_budget(**values):
            doc = json.loads(json.dumps(main))
            doc["evolution"]["budget"].update(values)
            return doc
        for why, cap, doc, words in (("no prices", 8.0, without("prices"), "prices no model"),
                                     ("no cache rates", 8.0, with_budget(prices=INPUT_OUTPUT), "cache_read_per"),
                                     ("no share", 8.0, without("max_dollars"), "sets no budget.max_dollars"),
                                     ("a share in words", 8.0, with_budget(max_dollars="1"), "positive number of"),
                                     ("a negative share", 8.0, with_budget(max_dollars=-1), "positive number of"),
                                     ("not strict", 8.0, without("strict"), "budget.strict true"),
                                     ("share over the cap", 8.0, with_budget(max_dollars=9.0), "above the cap"),
                                     ("no cap", float("nan"), main, "positive number")):
            with self.subTest(why):
                check = pilot.check_budget(cap, doc, 6)
                self.assertEqual(check.status, "FAIL")
                self.assertIn(words, check.detail)

    def test_file_names_paths_and_an_empty_table_on_any_system(self):
        self.assertEqual(pilot.file_name("GH:1"), "GH_1", "':' would name an NTFS stream, out of the scan's sight")
        self.assertEqual(pilot.file_name("LIB-1.a+b_c"), "LIB-1.a+b_c")
        for path in ("../x", "src/../../x", "..\\x", "src\\..\\x", "/etc", "\\x", "C:x", " "):
            self.assertFalse(pilot.relative_path(path), path)
        for path in ("src", "src/app", "src\\app", "a..b", ".github"):
            self.assertTrue(pilot.relative_path(path), path)
        self.assertEqual(pilot.table([]), [])

    def test_a_file_is_searched_whole_and_across_its_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "big.bin"
            path.write_bytes(b"x" * 1000 + b"needle-0123456789" + b"y" * 1000)
            self.assertTrue(pilot.holds(path, b"needle-0123456789", block=1005), "a match across two blocks")
            self.assertFalse(pilot.holds(path, b"needle-9", block=7))
            found, read, unread = pilot.files_holding("needle-0123456789", [path, Path(folder) / "gone"], limit=10)
            self.assertEqual((found, read, unread), ([], 0, 2), "over the limit, and unreadable: counted, not read")
            self.assertEqual(pilot.files_holding("needle-0123456789", [path], limit=None)[:2], ([path], 1))

    def test_the_gate_keeps_one_share_of_room_under_the_cap(self):
        self.assertIsNone(pilot.gate(0.0, 1.0, 8.0))
        self.assertIsNone(pilot.gate(7.0, 1.0, 8.0), "exactly at the cap is allowed")
        self.assertIn("cannot cover", pilot.gate(7.01, 1.0, 8.0))
        self.assertIn("unknown", pilot.gate(None, 1.0, 8.0), "an unpriced call stops the run: unknown is never zero")

    def test_the_report_must_compare_every_item_and_a_kept_check_is_a_warning(self):
        rule = {"met": False}

        def rows(checks, compared, not_run=()):
            reasons = []
            found = {"items": {"compared": compared}, "checks": checks, "not_run": list(not_run), "rule": rule}
            return [(r["status"], r["name"], r["detail"]) for r in pilot.report_rows(found, 3, reasons)], reasons

        found, reasons = rows({"unused": ["C"], "use_unknown": ["B"]}, 3)
        self.assertEqual(found, [("PASS", "report", "3 of 3 items compared; rule not met"),
                                 ("WARN", "unused", f"C: {experiment.CHECKS['unused']}"),
                                 ("WARN", "use_unknown", f"B: {experiment.CHECKS['use_unknown']}")])
        self.assertEqual(reasons, [], "a check that keeps an item fails nothing")
        for flag in ("arm_conflict", "contaminated", "missing_arm"):  # every exclusion fails, the API arm's included
            with self.subTest(flag):
                found, reasons = rows({flag: ["B"]}, 2)
                self.assertEqual(found, [("FAIL", "report", f"2 of 3 items compared; {flag}: B")])
                self.assertEqual(reasons, [f"the paired report did not compare every item: 2 of 3 items compared; "
                                           f"{flag}: B"])
        found, _ = rows({"missing_arm": ["A"]}, 0, not_run=["B", "C"])
        self.assertEqual(found, [("FAIL", "report", "0 of 3 items compared; missing_arm: A; not run: B, C")])
        self.assertEqual(pilot.report_rows(None, 3, []), [{"status": "FAIL", "name": "report",
                                                          "detail": "the report could not be read"}])


class DryRun(DryRunCase):
    def test_one_command_runs_the_canary_both_arms_the_review_and_the_report_with_every_metric_known(self):
        code, printed, receipt = self.dry_run()
        self.assertEqual((code, receipt["verdict"]), (0, "PASS"), printed)
        self.assertEqual(receipt["reasons"], [])
        self.assertEqual([c["status"] for c in receipt["preflight"]], ["PASS"] * len(receipt["preflight"]))
        self.assertEqual([c["name"] for c in receipt["preflight"]],
                         ["plan", "repository", "claude code", "credential lane", "githits token", "githits doctor",
                          "guard key", "budget cap", "contracts"])

        # The canary: one call, in the treatment's mode; the guard refused one lookup and passed the other.
        canary = receipt["canary"]
        self.assertEqual(canary["status"], "PASS", canary["detail"])
        self.assertEqual([(x["tool"], x["guard"], x["ok"]) for x in canary["lookups"]],
                         [("Bash", "email", False), ("githits.search", "pass", True)])
        self.assertIn("the guard refused 1 lookup(s) (email) and passed 1 that reached GitHits", canary["detail"])
        self.assertEqual(canary["spend"], COST)

        # Every item in both arms, from the one base, in the seeded order, each thread labelled before its candidate.
        expected = pilot.schedule(["LIB-1", "LIB-2", "LIB-3"], SEED)
        self.assertEqual([(s["item"], s["arm"]) for s in receipt["schedule"]], expected)
        threads = receipt["threads"]
        self.assertEqual([(t["item"], t["arm"]) for t in threads], expected)
        self.assertEqual(receipt["commit"]["base"], self.head)
        for entry in threads:
            self.assertEqual(entry["base"], self.head, "every thread started from the base")
            self.assertTrue(entry["accepted"], entry)
            self.assertEqual(entry["unknown"], [], entry["metrics"])
            self.assertEqual(set(entry["metrics"]), set(metrics.METRICS))
            self.assertEqual((entry["metrics"]["agent_time"]["source"], entry["metrics"]["delivered"]["source"]),
                             ("time_to_pass", "reviewed"))
            self.assertIs(entry["metrics"]["delivered"]["value"], True, "accepted and passed the blind review")
            self.assertEqual(entry["metrics"]["cost"]["value"], COST, "one call, priced by the host")
            self.assertEqual(entry["metrics"]["human_interventions"]["value"], 0, "the runner is no person")
            self.assertEqual(entry["strict"]["agent_time"]["known"], False, "to merge: no merge in a dry run")
            on = entry["arm"] == "on"
            self.assertEqual(entry["metrics"]["githits_lookups"]["value"], 1 if on else 0)
            self.assertEqual(entry["lookups_blocked"]["value"], 1 if on else 0)
            self.assertEqual(entry["harness"]["lane"], ["api"])
            self.assertEqual(entry["harness"].get("githits_route"), ["api"] if on else None)
            self.assertEqual(entry["review"]["verdict"], "pass")
        self.assertEqual(sorted(t["review"]["id"] for t in threads), [f"R{n}" for n in range(1, 7)])
        for entry in threads:
            self.assertTrue((self.out / "review" / f"{entry['review']['id']}.diff").is_file())

        # The commands: exactly the documented ones, the label before the proposal, --githits in the treatment only.
        commands = [c["argv"] for c in receipt["commands"]]
        proposals = [argv for argv in commands if "propose" in argv]
        self.assertEqual(len(proposals), 7, "the canary and six runs")
        for argv in proposals:
            start = argv.index("propose")
            self.assertEqual(argv[start:start + 3], ["propose", "--claude-code", "--bare"])
        tools = [argv[argv.index("--allowed-tools") + 1] for argv in proposals]
        self.assertEqual(tools, ["Read,Edit,Write,Glob,Grep"] + ["Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"] * 6,
                         "the pilot's tools in both arms; the canary without the web, so it can only use the launcher")
        self.assertTrue(all(argv[argv.index("--githits-arm") + 1] == "on" for argv in proposals if "--githits" in argv))
        self.assertEqual(sum("--githits" in argv for argv in proposals), 4, "the canary and the three treatment runs")
        for entry in threads:
            mine = [i for i, argv in enumerate(commands) if entry["thread"] in argv]
            kinds = [next(w for w in ("experiment", "propose", "run", "export", "diff", "review") if w in commands[i])
                     for i in mine]
            self.assertEqual(kinds[:4], ["experiment", "propose", "run", "export"], kinds)
            self.assertEqual("--githits" in commands[mine[1]], entry["arm"] == "on")
        report = commands[-2]
        self.assertEqual(report[:2], ["experiment", "report"])
        for flag, value in (("--items", str(Path("<run>") / "items.txt")), ("--delivered", "review")):
            self.assertEqual(report[report.index(flag) + 1], value)
        commands = json.dumps(receipt["commands"])
        for form in (str(self.out), json.dumps(str(self.out))[1:-1], self.out.as_posix()):  # as JSON writes it too
            self.assertNotIn(form, commands, "the run's own path stays on the machine")

        # GitHits saw the doctor, the canary's clean lookup and one per treatment run, with the arm's headers.
        self.assertEqual([r["path"] for r in self.fake.requests], ["/v1/languages"] + ["/v1/searches"] * 4)
        agents = {r["headers"].get("x-githits-agent") for r in self.fake.requests[1:]}
        self.assertEqual(agents, {"syberfactory/on"})
        sessions = {r["headers"]["x-githits-session-id"] for r in self.fake.requests[2:]}
        self.assertEqual(sessions, {f"{t['thread']}-on" for t in threads if t["arm"] == "on"})

        # The report ran on the registered list and compared every item: it read each treatment's GitHits from the
        # host's launcher ledger (#141), so no API-route item is excluded as arm_conflict, and no check fired.
        self.assertEqual((self.out / "items.txt").read_text(encoding="utf-8").splitlines()[1:],
                         ["LIB-1 needs_lookup", "LIB-2 needs_lookup", "LIB-3 routine"])
        found = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(found["items"], {"labelled": 3, "compared": 3, "excluded": 0})
        self.assertEqual((found["checks"], found["not_run"]), ({}, []))
        lookups = found["metrics"]["githits_lookups"]
        self.assertEqual((lookups["pairs"], lookups["baseline_mean"], lookups["treatment_mean"]), (3, 0.0, 1.0),
                         "the report pairs the treatment's lookups with the baseline's none, item by item")
        self.assertEqual(receipt["report"]["compared"], 3)
        self.assertIn("PASS  report: 3 of 3 items compared; rule not met", printed)
        self.assertTrue((self.out / "report.md").read_text(encoding="utf-8").startswith("| Metric |"))

        # Spend: the host's own, every call priced, under the cap.
        spend = receipt["spend"]
        self.assertEqual(spend["total"], round(7 * COST, 6))
        self.assertEqual(spend["total"], round(sum(spend["by_thread"].values()), 6))
        self.assertTrue(spend["within_cap"])
        self.assertEqual(spend["thread_budget"], round(8 / 7, 4))
        self.assertIn("spend   $0.5313 of $8.00 (canary $0.0759)", printed)

        # The table, the receipt's facts, and no credential or objective anywhere it should not be.
        self.assertIn("pilot dry run: PASS", printed)
        self.assertEqual(receipt["versions"]["claude_code"], "2.1.309")
        self.assertEqual(receipt["seed"], SEED)
        self.assertEqual(receipt["credential_scan"]["found"], [])
        text = (self.out / "receipt.json").read_text(encoding="utf-8")
        for secret in (self.token, self.api_key):
            self.assertNotIn(secret, printed)
            for path in self.out.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes(), path)
        self.assertNotIn("Add a CSV export for report rows", text, "objectives leave the machine only as digests")
        self.assertNotIn("Add a CSV export for report rows", (self.out / "report.md").read_text(encoding="utf-8"))
        self.assertTrue(git(self.root, "status", "--porcelain") == "", "the run's directory is outside the tree")

    def test_a_change_that_never_passes_has_no_time_to_pass_and_fails_the_bar(self):
        self.claude("broken", first=BROKEN)  # the host's checks fail every call's change
        code, printed, receipt = self.dry_run(plan=self.plan(max_calls=1))
        self.assertEqual((code, receipt["verdict"]), (1, "FAIL"))
        self.assertEqual([t["accepted"] for t in receipt["threads"]], [False] * 6)
        self.assertEqual([t["unknown"] for t in receipt["threads"]], [["agent_time"]] * 6,
                         "not accepted: delivered is known (no), and so is everything but the time to a passing change")
        self.assertTrue(all(t["metrics"]["delivered"]["value"] is False for t in receipt["threads"]))
        self.assertNotIn("Blind review", printed, "nothing was accepted, so nothing is reviewed")
        self.assertIn("FAIL  nine metrics: known for 0 of 6 threads", printed)
        why = [reason for reason in receipt["reasons"] if ": not accepted: " in reason]
        self.assertEqual(len(why), 6, "each thread says why it has no time to a passing change")
        self.assertIn("none of its 1 candidate(s) passed the host's checks", why[0])

    def test_a_skipped_review_leaves_delivered_unknown_and_fails_the_bar(self):
        code, printed, receipt = self.dry_run(ask=answers("", "maybe", "skip", *["pass"] * 5))
        self.assertEqual((code, receipt["verdict"]), (1, "FAIL"))
        unknown = [(t["review"]["id"], t["unknown"]) for t in receipt["threads"] if t["unknown"]]
        self.assertEqual(unknown, [("R1", ["delivered"])], "an empty or unknown answer asks again; skip leaves it")
        self.assertIn("FAIL  nine metrics: known for 5 of 6 threads", printed)
        self.assertTrue(any("unknown delivered" in reason for reason in receipt["reasons"]))


class Canary(DryRunCase):
    def test_a_guard_that_does_not_fire_stops_everything_after_one_call(self):
        self.claude("deaf", hooks=False)  # a Claude Code that does not run the --settings hook
        code, printed, receipt = self.dry_run()
        self.assertEqual((code, receipt["verdict"]), (1, "FAIL"))
        self.assertEqual(receipt["canary"]["status"], "FAIL")
        self.assertIn("the query guard did not fire", receipt["canary"]["detail"])
        self.assertEqual(receipt["threads"], [], "no run was started")
        self.assertEqual(len(self.runs("deaf")), 1, "the canary was the only call")
        self.assertIn("unguarded", {x["guard"] for x in receipt["canary"]["lookups"]})

    def test_a_model_the_plan_does_not_price_stops_the_run_at_the_canary(self):
        prices = {"another-model": {"input_per_million": 3, "output_per_million": 15, "cache_read_per_million": 0.3,
                                    "cache_creation_per_million": 3.75}}
        code, _, receipt = self.dry_run(plan=self.plan(prices=prices))
        self.assertEqual((code, receipt["canary"]["status"]), (1, "FAIL"))
        self.assertIn("claude-opus-5, which the plan does not price", receipt["canary"]["detail"])
        self.assertEqual(receipt["threads"], [])
        self.assertIsNone(receipt["spend"]["total"], "unknown, never zero")

    def test_a_canary_without_a_lookup_is_inconclusive(self):
        self.claude("silent", lookups=[])
        code, _, receipt = self.dry_run()
        self.assertEqual((code, receipt["canary"]["status"]), (1, "FAIL"))
        self.assertIn("inconclusive", receipt["canary"]["detail"])

    def test_a_canary_needs_both_a_refusal_and_a_lookup_that_reached_githits(self):
        # A model that leaves the address out proves no refusal; a launcher that never runs proves no GitHits. Each
        # would pass a weaker test and send the arms out unguarded or without GitHits.
        for number, (lookups, words) in enumerate(((LOOKUPS[1:], "no lookup the guard refused"),
                                                   (LOOKUPS[:1], "no lookup the guard passed that reached GitHits"))):
            with self.subTest(words):
                self.claude(f"half-{number}", lookups=lookups)
                code, _, receipt = self.dry_run(out=f"half-{number}")
                self.assertEqual((code, receipt["canary"]["status"]), (1, "FAIL"))
                self.assertIn(f"inconclusive: {words}", receipt["canary"]["detail"])
                self.assertEqual(receipt["threads"], [], "no run was started")
                self.assertEqual(len(self.runs(self.bin)), 1, "the canary was the only call")


class Budget(DryRunCase):
    def test_no_run_starts_once_another_could_break_the_cap(self):
        # The canary and run 1 cost 0.0759 each; a share of 0.10 then no longer fits under 0.20.
        code, printed, receipt = self.dry_run("--budget", "0.2", "--thread-budget", "0.1")
        self.assertEqual((code, receipt["verdict"]), (1, "FAIL"))
        ran = [t for t in receipt["threads"] if t["thread"]]
        self.assertEqual(len(ran), 1)
        self.assertTrue(all("budget: stopped before run 2 of 6" in t["skipped"] for t in receipt["threads"][1:]))
        self.assertEqual(receipt["spend"]["total"], round(2 * COST, 6))
        self.assertLessEqual(receipt["spend"]["total"], 0.2)
        self.assertEqual(len(self.runs(self.bin)), 2, "the canary and one run: no call after the cap was near")
        contract = json.loads((self.out / "home" / "contracts" / "repo-change.v1.json").read_text(encoding="utf-8"))
        self.assertEqual((contract["evolution"]["budget"]["max_dollars"], contract["evolution"]["budget"]["strict"]),
                         (0.1, True), "the host's own meter holds each thread to its share")


    def test_a_call_that_costs_more_than_its_share_leaves_its_thread_unaccepted_and_says_why(self):
        code, _, receipt = self.dry_run("--thread-budget", "0.05")  # each call costs 0.0759
        self.assertEqual((code, receipt["verdict"]), (1, "FAIL"))
        self.assertEqual([t["accepted"] for t in receipt["threads"]], [False] * 6)
        why = [reason for reason in receipt["reasons"] if ": not accepted: " in reason]
        self.assertEqual(len(why), 6)
        self.assertIn("a candidate passed the host's checks", why[0])
        self.assertIn("search_over_budget", why[0], "the host's meter refused it: the share is the reason")


class FailedCall(DryRunCase):
    def test_a_call_that_dies_after_it_ran_makes_the_spend_unknown_and_stops_the_runs(self):
        # Claude Code dies after its lookups and before its result: claude_code raises provider_failed before its
        # meter prices the call, so what the call spent is in no record. The total is unknown, never that much less.
        arms = pilot.DryRun.arms

        def dying(dry_run):
            self.claude("dying", fail_after_lookups=3)
            return arms(dry_run)
        with mock.patch.object(pilot.DryRun, "arms", dying):
            code, printed, receipt = self.dry_run()
        self.assertEqual((code, receipt["verdict"], receipt["canary"]["status"]), (1, "FAIL", "PASS"))
        first, *rest = receipt["threads"]
        self.assertEqual(first["search"]["error"], "provider_failed")
        self.assertEqual(first["metrics"]["cost"]["known"], False)
        self.assertIn("provider_failed after Claude Code ran", first["metrics"]["cost"]["note"])
        self.assertTrue(all(entry["thread"] is None and "no further run" in entry["skipped"] for entry in rest))
        self.assertEqual(sum("propose" in c["argv"] for c in receipt["commands"]), 2,
                         "the canary and run 1: no call after the one the meter missed")
        self.assertIsNone(receipt["spend"]["total"], "unknown, not the canary's dollars")
        self.assertIsNone(receipt["spend"]["by_thread"][first["thread"]])
        self.assertIn("FAIL  spend: unknown", printed)
        self.assertTrue(any(reason.startswith("run 1 (") and ": not accepted: " in reason
                            and "its search ended provider_failed" in reason for reason in receipt["reasons"]))

    def test_an_export_that_cannot_be_read_keeps_the_one_before_it(self):
        dry_run = pilot.DryRun.__new__(pilot.DryRun)
        dry_run.receipt, dry_run.exports = {"reasons": []}, {}
        self.assertEqual(dry_run.keep("t" * 36, None), {})
        self.assertIsNone(dry_run.spent(), "a thread never read has an unknown spend, not $0")
        dry_run.keep("t" * 36, {"events": [{"kind": "x", "case_id": "t", "seq": 1, "body": {}}]})
        self.assertEqual(dry_run.keep("t" * 36, None)["events"][0]["kind"], "x", "a failed export keeps the earlier")
        self.assertEqual(len(dry_run.receipt["reasons"]), 2)
        self.assertIn("the one before it is kept", dry_run.receipt["reasons"][1])


class Preflight(DryRunCase):
    def refused(self, why, *flags, plan=None):
        self.out_count = getattr(self, "out_count", 0) + 1
        code, printed, receipt = self.dry_run(*flags, plan=plan, out=f"refused-{self.out_count}")
        self.assertEqual((code, receipt["verdict"]), (2, "REFUSED"), why)
        self.assertEqual(self.runs(self.bin), [], f"{why}: no Claude Code call ran")
        self.assertFalse((self.out / "home").exists(), f"{why}: refused before the run's home, or any thread, existed")
        self.assertNotIn(self.token, printed)
        return receipt, printed

    def test_each_precondition_refuses_the_start(self):
        with self.subTest("an old Claude Code"):
            self.claude("old", version="2.1.294 (Claude Code)")
            receipt, _ = self.refused("old")
            self.assertIn("older than 2.1.295", self.check(receipt, "claude code")["detail"])
            self.claude("claude")
        with self.subTest("a third-party routing switch"), mock.patch.dict(os.environ, {
                "ANTHROPIC_BASE_URL": "https://gateway.example.test", "CLAUDE_CODE_USE_BEDROCK": "1"}):
            receipt, _ = self.refused("routed")
            detail = self.check(receipt, "credential lane")["detail"]
            self.assertIn("ANTHROPIC_BASE_URL, CLAUDE_CODE_USE_BEDROCK set", detail)
        with self.subTest("no API key"), mock.patch.dict(os.environ, {pilot.API_KEY_ENV: ""}):
            receipt, _ = self.refused("no key")
            self.assertIn("ANTHROPIC_API_KEY is not set", self.check(receipt, "credential lane")["detail"])
        with self.subTest("no GitHits token"), mock.patch.dict(os.environ, {githits.TOKEN_ENV: ""}):
            receipt, _ = self.refused("no token")
            self.assertEqual(self.check(receipt, "githits token")["status"], "FAIL")
            self.assertIn("no_token", self.check(receipt, "githits doctor")["detail"])
        with self.subTest("the token in a file of the repository"):
            note = self.root / "notes.txt"
            note.write_text(f"GITHITS_API_TOKEN={self.token}\n", encoding="utf-8")
            receipt, printed = self.refused("token in a file")
            self.assertIn("notes.txt", self.check(receipt, "githits token")["detail"])
            self.assertNotIn(self.token, json.dumps(receipt))
            note.unlink()
        with self.subTest("the token in an ignored .env"):
            (self.root / ".gitignore").write_text(".env\n", encoding="utf-8")
            (self.root / ".env").write_text(f"export GITHITS_API_TOKEN={self.token}\n", encoding="utf-8")
            receipt, _ = self.refused("token in .env")
            self.assertIn(".env", self.check(receipt, "githits token")["detail"])
            (self.root / ".env").unlink()
            (self.root / ".gitignore").unlink()
        with self.subTest("the doctor not ready"):
            self.fake.serve("/v1/languages", {"code": "AUTHENTICATION_REQUIRED", "title": "x"}, 401,
                            {"Content-Type": "application/problem+json"})
            receipt, _ = self.refused("doctor")
            self.assertIn("auth (AUTHENTICATION_REQUIRED)", self.check(receipt, "githits doctor")["detail"])
            del self.fake.script["/v1/languages"]
        with self.subTest("Claude Code's subprocess scrub"), mock.patch.dict(os.environ, {pilot.SCRUB_ENV: "1"}):
            receipt, _ = self.refused("scrub")
            self.assertIn(pilot.SCRUB_ENV, self.check(receipt, "guard key")["detail"])
        with self.subTest("no budget"):
            receipt, _ = self.refused("budget", "--budget", "0")
            self.assertIn("--budget must be a positive", self.check(receipt, "budget cap")["detail"])
        with self.subTest("a thread budget over the cap"):
            receipt, _ = self.refused("thread budget", "--budget", "1", "--thread-budget", "2")
            self.assertIn("at most the cap", self.check(receipt, "budget cap")["detail"])
        with self.subTest("HEAD is not the base"):
            git(self.root, "commit", "--allow-empty", "-qm", "later")
            receipt, _ = self.refused("base", "--base", self.head)
            self.assertIn(f"not the base {self.head[:10]}", self.check(receipt, "repository")["detail"])
        with self.subTest("a plan that is not a dry run"):
            receipt, _ = self.refused("plan", plan=self.plan(items=2, name="small"))
            self.assertIn("3 to 5 work items", self.check(receipt, "plan")["detail"])

    def test_an_interrupt_before_the_first_check_still_writes_the_receipt_and_prints_the_table(self):
        with mock.patch.object(pilot, "check_repository", side_effect=KeyboardInterrupt):
            code, printed, receipt = self.dry_run()
        self.assertEqual((code, receipt["verdict"], receipt["preflight"]), (1, "ABORTED", []))
        self.assertIn("pilot dry run: ABORTED", printed)
        self.assertNotIn("Traceback", printed)

    def test_the_guard_key_check_runs_the_hosts_hook_as_claude_code_does(self):
        check = pilot.check_guard_key(os.environ)
        self.assertEqual(check.status, "PASS", check.detail)
        with mock.patch.object(query_guard, "authentic", return_value=False):
            self.assertEqual(pilot.check_guard_key(os.environ).status, "FAIL", "a decision that does not verify fails")

    def test_the_operator_must_confirm_before_anything_is_spent(self):
        self.out = self.root.parent / "unconfirmed"
        with contextlib.redirect_stdout(io.StringIO()):
            code = pilot.main(["--plan", str(self.plan()), "--repo", str(self.root), "--out", str(self.out)],
                              ask=answers("n"), runner=in_process)
        receipt = json.loads((self.out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual((code, receipt["verdict"]), (2, "REFUSED"))
        self.assertEqual(self.runs(self.bin), [])


class OneCommand(DryRunCase):
    def test_the_script_as_the_operator_runs_it(self):
        """``python scripts/pilot_dry_run.py --plan ...``: every ``syberlabs`` command a subprocess of this Python,
        the review answered on standard input."""
        out = self.root.parent / "one-command"
        done = subprocess.run([sys.executable, str(SCRIPT), "--plan", str(self.plan()), "--repo", str(self.root),
                               "--out", str(out), "--seed", str(SEED), "--yes"], input=b"pass\n" * 6,
                              capture_output=True, timeout=900, cwd=str(self.root.parent))
        printed = done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")
        self.assertEqual(done.returncode, 0, printed[-3000:])
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["verdict"], "PASS")
        self.assertEqual([t["unknown"] for t in receipt["threads"]], [[]] * 6)
        self.assertIn("pilot dry run: PASS", printed)
        for secret in (self.token, self.api_key):
            self.assertNotIn(secret, printed)
        log = (out / "commands.log").read_text(encoding="utf-8")
        self.assertIn("$ syberlabs githits doctor --json", log)
        self.assertEqual(log.count(" propose --claude-code --bare "), 7, "the canary and six runs")
        self.assertEqual(len(self.fake.requests), 5, "the doctor, the canary's clean lookup, three treatment runs")


if __name__ == "__main__":
    unittest.main()
