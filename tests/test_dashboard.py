"""The operator page and the refusal report: computed from the journal, escaped, and read-only."""

import base64
import contextlib
import io
import json
import re

from syberlabs import dashboard, refusals
from syberlabs.admission import rule_order
from tests.test_build_thread import GOOD, RepoCase

BROKEN = {**GOOD, "src/report.py": "def rows():\n    return []\n"}
OBJECTIVE = "Add a CSV export <script>alert(1)</script> & keep the header"
ESCAPED = "Add a CSV export &lt;script&gt;alert(1)&lt;/script&gt; &amp; keep the header"


class Journaled(RepoCase):
    """One thread: c1 passes and is accepted, c2 fails its check and is refused."""

    def setUp(self):
        super().setUp()
        self.kit_ = self.kit()
        self.thread = self.kit_.start(OBJECTIVE, paths=["src", "tests"])
        self.thread.propose(changes=GOOD, message="CSV export")
        self.thread.propose(changes=BROKEN, message="breaks report.rows")
        self.thread.check("c1")
        self.thread.check("c2")
        refused = self.thread.accept("c2")
        self.assertEqual((refused.status, refused.reason), ("denied", "candidate_check_failed:tests"))
        self.assertEqual(self.thread.accept("c1").status, "succeeded")
        self.home = self.root / ".syberlabs"
        self.journal_file = self.home / "journal" / "threads" / f"{self.thread.id}.jsonl"

    def journal_state(self):
        return len(self.thread.history()), self.journal_file.read_bytes()


class Refusals(Journaled):
    def test_every_refusal_with_its_rule_reason_candidate_and_evidence(self):
        before = self.journal_state()
        [row] = refusals.refusals(self.kit_)
        self.assertEqual((row["thread"], row["candidate"], row["action"], row["phase"]),
                         (self.thread.id, "c2", "accept_change", "propose"))
        self.assertEqual((row["reason"], row["rule"]), ("candidate_check_failed:tests", "candidate.promotable"))
        self.assertIn(row["rule"], rule_order())
        self.assertEqual((row["actor"], row["origin"], row["provider"]), ("dev@example.test", "human", "patch@1"))
        self.assertEqual(row["commit"], self.thread.candidates()[1].commit)
        [check] = row["checks"]
        self.assertEqual((check["name"], check["state"]), ("tests", "failed"))
        self.assertRegex(check["output_digest"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(check["exit_code"], 0)
        self.assertIsInstance(check["duration_ms"], int)
        self.assertRegex(row["evaluation_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(row["event_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(row["objective"], OBJECTIVE)
        self.assertEqual(self.journal_state(), before, "the refusal report must not record anything")
        self.assertEqual(refusals.refusals(self.kit_, thread="no-such-thread"), [])

    def test_text_and_json_from_the_terminal(self):
        text = refusals.render_text(refusals.refusals(self.kit_))
        self.assertIn("accept_change c2: candidate_check_failed:tests  (rule candidate.promotable", text)
        self.assertIn("failed    tests", text)
        self.assertTrue(text.endswith("1 refusal\n"))
        self.assertEqual(refusals.render_text([]), "no refusals recorded\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = refusals.main(["--home", str(self.home), "--repo", str(self.root), "--json"])
        self.assertEqual(code, 0)
        [row] = json.loads(out.getvalue())
        self.assertEqual((row["candidate"], row["reason"]), ("c2", "candidate_check_failed:tests"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            refusals.main(["--home", str(self.home), "--repo", str(self.root), "--thread", self.thread.id[:8]])
        self.assertIn("candidate.promotable", out.getvalue())


class Dashboard(Journaled):
    def test_model_is_computed_from_the_journal(self):
        model = dashboard.collect(self.kit_, now=1_700_000_000)
        self.assertEqual(model["summary"], {
            "threads": {"total": 1, "complete": 1},
            "candidates": {"proposed": 2, "accepted": 1, "refused": 1},
            "refusals": 1,
            "effects": {"claimed": 1, "applied": 1, "not_applied": 0, "pending": 0},
        })
        [thread] = model["threads"]
        events = self.thread.history()
        self.assertEqual((thread["id"], thread["objective"], thread["state"], thread["accepted"], thread["act_class"]),
                         (self.thread.id, OBJECTIVE, "complete", "c1", "R"))
        self.assertEqual((thread["head"], thread["last_at"], thread["events"], thread["chain_valid"]),
                         (events[-1]["hash"], events[-1]["at"], len(events), True))
        c1, c2 = thread["candidates"]
        self.assertEqual((c1["id"], c1["provider"], c1["evaluation"], c1["promotion"], c1["authoritative"]),
                         ("c1", "patch@1", "passed", "promoted", True))
        self.assertEqual((c2["id"], c2["evaluation"], c2["promotion"], c2["reason"], c2["authoritative"]),
                         ("c2", "failed", "denied", "candidate_check_failed:tests", False))
        self.assertEqual([(c["name"], c["state"]) for c in c1["checks"]], [("tests", "passed")])
        self.assertIsInstance(c1["checks"][0]["duration_ms"], int)
        [effect] = model["effects"]
        self.assertEqual((effect["action"], effect["candidate"], effect["verdict"], effect["detail"]),
                         ("accept_change", "c1", "applied", "effect_succeeded"))
        self.assertEqual((effect["external_id"], effect["destination"]), (c1["commit"], self.thread.target_ref))
        self.assertEqual(model["generated_at"], 1_700_000_000_000_000)
        self.assertEqual(model["certificates"], [])

    def test_page_names_the_thread_and_the_refusal_escapes_free_text_and_records_nothing(self):
        before = self.journal_state()
        out = self.root.parent / "pages" / "dashboard.html"
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = dashboard.main(["--home", str(self.home), "--repo", str(self.root), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("1 threads, 2 candidates, 1 refusals, 1 effects", printed.getvalue())
        page = out.read_text(encoding="utf-8")
        self.assertIn(self.thread.id, page)
        self.assertIn("candidate_check_failed:tests", page)
        self.assertIn("candidate.promotable", page)
        self.assertIn(ESCAPED, page)
        self.assertNotIn("<script", page.lower(), "the page carries no script, not even an escaped-looking one")
        self.assertNotIn(OBJECTIVE, page)
        self.assertIn("patch@1", page)
        self.assertIn("accept_change", page)
        self.assertIn(self.thread.history()[-1]["hash"], page, "the footer names the chain head")
        self.assertIn(str(self.kit_.journal.home), page.replace("&#x27;", "'"))
        self.assertIn("prefers-color-scheme", page)
        self.assertIn('name="viewport"', page)
        self.assertNotIn("<link", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertEqual(self.journal_state(), before, "rendering the page must not append an event")

    def test_certificates_are_listed_when_present(self):
        folder = self.home / "certificates"
        folder.mkdir()
        payload = {"thread": self.thread.id, "result": {"candidate": "c1"}, "issued_at": 1_700_000_000_000_000}
        envelope = {"payloadType": "x", "payload": base64.b64encode(json.dumps(payload).encode()).decode(), "signatures": []}
        (folder / f"{self.thread.id[:8]}-c1.json").write_text(json.dumps(envelope))
        (folder / "broken.json").write_text("{not json")
        model = dashboard.collect(self.kit_)
        self.assertEqual(sorted((c["file"], c["readable"], c["candidate"]) for c in model["certificates"]),
                         sorted([("broken.json", False, None), (f"{self.thread.id[:8]}-c1.json", True, "c1")]))
        page = dashboard.render_html(model)
        self.assertIn("<h2>Certificates</h2>", page)
        self.assertIn("unreadable envelope", page)
        self.assertIn("2023-11-14 22:13:20 UTC", page)

    def test_unreadable_thread_is_shown_not_hidden(self):
        lines = self.journal_file.read_text().splitlines()
        lines[1] = lines[1].replace("CSV export", "Delete the database")
        self.journal_file.write_text("\n".join(lines) + "\n")
        model = dashboard.collect(self.kit())
        [thread] = model["threads"]
        self.assertEqual((thread["state"], thread["candidates"], thread["chain_valid"]), ("unreadable (journal_corrupt)", [], False))
        self.assertIn("unreadable (journal_corrupt)", dashboard.render_html(model))


class Empty(RepoCase):
    def test_an_empty_home_renders_a_page(self):
        kit = self.kit()
        page = dashboard.render(kit)
        self.assertIn("No threads in this journal.", page)
        self.assertIn("The kernel refused nothing.", page)
        self.assertEqual(len(re.findall(r"<h2>", page)), 5, "no certificates section without certificates")
