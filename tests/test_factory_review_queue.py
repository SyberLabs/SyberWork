"""The queue for the person who merges: ready work in an order that says why, and a reading order for each change.
Offline on the fixture app: plans a person wrote, a patch as the proposer, nothing pushed or merged."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from syberlabs.build import Kit
from syberlabs.factory import cli as factory_cli
from syberlabs.factory import intake, review_queue, work
from syberlabs.providers import PatchProvider
from tests.test_factory_work import AGENT, CHECKS, HUMAN, OTHER, Scratch, git

REQUESTS = {
    "acme/app#1": ("rules crash on quantity zero",
                   "Steps: run the order path with quantity 0.\nExpected: no order is posted.\nInstead the rules module raises ZeroDivisionError."),
    "acme/app#2": ("effects fail when the ledger file is absent",
                   "Steps: post one order with no ledger file present.\nExpected: the effects module creates it.\nInstead it fails with FileNotFoundError."),
    "acme/app#3": ("ingest wrong for an empty line",
                   "Steps: feed the ingest module a file with one empty line.\nExpected: the line is skipped.\nInstead the parser returns a wrong record."),
    "acme/app#4": ("main exits with a traceback on a missing fixture",
                   "Steps: start main with a fixture path that does not exist.\nExpected: one clear error line.\nInstead main fails with a traceback."),
}


def plan_of(files, new_files=()):
    return {"files": list(files), "new_files": list(new_files), "steps": ["Make the change the request asks for."], "risks": []}


class Queue(Scratch):
    def ticket(self, ref):
        return work.state(self.home, ref)["ticket_id"]

    def planned_by_hand(self, ref, files, new_files=()):
        self.triaged(ref, *REQUESTS[ref])
        found = work.make_plan(self.home, ref, repo_root=self.project, written=(HUMAN, plan_of(files, new_files)), limits={"max_files": 12})
        self.assertEqual(found["state"]["status"], "planned", found["planned"])

    def ready(self, ref, files, new_files=(), by=AGENT):
        """Plan, claim, start, propose a change to every planned path, accept, settle."""
        self.planned_by_hand(ref, files, new_files)
        work.claim(self.home, ref, by)
        found = work.start(self.home, ref, by, repo_root=self.project, kit_home=".syberlabs", checks=CHECKS)
        kit = Kit.local(self.project / ".syberlabs", self.project)
        try:
            thread = kit.open(found["thread"])
            changes = {path: (self.project / path).read_text(encoding="utf-8") + f"\n# {ref}\n" for path in files}
            changes.update({path: f"# {ref}\n" for path in new_files})
            [candidate] = thread.propose(PatchProvider(changes))
            thread.check(candidate.id)
            self.assertEqual(thread.accept(candidate.id).status, "succeeded")
        finally:
            kit.close()
        self.assertEqual(work.settle(self.home, ref, repo_root=self.project, kit_home=".syberlabs")["status"], "ready")
        return found["thread"]

    def rows(self):
        return review_queue.queue(self.home, repo_root=self.project)


class ReadingOrder(Scratch):
    def test_a_component_is_read_after_the_ones_it_depends_on(self):
        graph = self.home.latest_space()
        paths = ["fxapp/main.py", "fxapp/ingest.py", "tests/test_x.py", "fxapp/effects.py", "fxapp/rules.py", "README.md", "fxapp/variants.py"]
        self.assertEqual(review_queue.reading_order(graph, paths), [
            {"layer": 1, "component": "rules", "paths": ["fxapp/rules.py"]},
            {"layer": 2, "component": "effects", "paths": ["fxapp/effects.py"]},
            {"layer": 3, "component": "ingest", "paths": ["fxapp/ingest.py"]},
            {"layer": 4, "component": "main", "paths": ["fxapp/main.py", "fxapp/variants.py"]},
            {"layer": 5, "component": None, "paths": ["README.md", "tests/test_x.py"]}])
        self.assertEqual(review_queue.reading_order(graph, ["fxapp/main.py"]), [{"layer": 1, "component": "main", "paths": ["fxapp/main.py"]}])
        self.assertEqual(review_queue.reading_order(None, ["b.py", "a.py"]), [{"layer": 1, "component": None, "paths": ["a.py", "b.py"]}])
        self.assertEqual(review_queue.reading_order(graph, []), [])

    def test_a_cycle_and_a_tie_go_by_name(self):
        graph = {"files": {}, "components": {name: {"paths": [f"{name}/**"]} for name in ("a", "b", "c", "d")},
                 "depends_on": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}, {"from": "c", "to": "a"}, {"from": "d", "to": "d"}]}
        order = [layer["component"] for layer in review_queue.reading_order(graph, ["c/x.py", "b/x.py", "a/x.py", "d/x.py"])]
        self.assertEqual(order, ["d", "a", "b", "c"], "d needs nothing; a and b need each other, so the first by name goes; c follows a")


class Ordering(Queue):
    def test_nothing_ready_is_an_empty_queue(self):
        self.planned_by_hand("acme/app#1", ["fxapp/rules.py"])
        self.assertEqual(self.rows(), [])
        self.assertIn("nothing is ready", review_queue.text([]))

    def test_the_change_others_wait_on_is_first_then_the_smaller_then_the_older(self):
        self.ready("acme/app#3", ["fxapp/ingest.py", "fxapp/effects.py"])            # two files, ready first
        self.ready("acme/app#1", ["fxapp/rules.py"], ["tests/test_rules_request.py"])  # two files, ready second
        self.ready("acme/app#4", ["fxapp/main.py"])                                   # one file, ready last
        rows = self.rows()
        self.assertEqual(rows[0]["source"]["ref"], "acme/app#4", "the fewest files first")
        keys = [(row["order"]["files"], row["order"]["diff_bytes"], row["order"]["waiting_since"]) for row in rows]
        self.assertEqual(keys, sorted(keys), "then the smaller diff")
        self.assertEqual([row["position"] for row in rows], [1, 2, 3])
        self.assertEqual([row["overlaps"] for row in rows], [[], [], []], "three changes to different files")
        self.planned_by_hand("acme/app#2", ["fxapp/effects.py", "fxapp/rules.py"])    # waits on #3's effects.py and on #1's rules.py
        waiting = self.ticket("acme/app#2")
        rows = self.rows()
        self.assertEqual(rows[2]["source"]["ref"], "acme/app#4",
                         "a change another ticket is waiting on is read before a smaller one nobody waits on")
        self.assertEqual([row["blocks"] for row in rows], [[waiting], [waiting], []])
        self.assertEqual(rows[0]["order"]["blocks"], 1)
        self.assertEqual(rows[2]["order"], {"blocks": 0, "files": 1, "diff_bytes": rows[2]["diff_bytes"], "waiting_since": rows[2]["order"]["waiting_since"]})

    def test_the_keys_decide_in_their_order(self):
        def row(name, blocks, files, size, since, found=True):
            return {"name": name, "found": found, "order": {"blocks": blocks, "files": files, "diff_bytes": size, "waiting_since": since}}

        rows = [row("older", 0, 2, 500, 10), row("newer", 0, 2, 500, 20), row("smaller", 0, 2, 400, 30), row("fewer", 0, 1, 900, 40),
                row("waited on", 1, 9, 9000, 50), row("gone", 3, 1, None, 5, found=False)]
        self.assertEqual([r["name"] for r in sorted(reversed(rows), key=review_queue.sort_key)],
                         ["waited on", "fewer", "smaller", "older", "newer", "gone"])
        self.assertEqual(review_queue.ORDER, ("blocks", "files", "diff_bytes", "waiting_since"))

    def test_a_row_says_what_was_accepted_and_how_to_read_it(self):
        thread = self.ready("acme/app#1", ["fxapp/main.py", "fxapp/rules.py"], ["tests/test_rules_request.py"])
        [row] = self.rows()
        self.assertEqual((row["title"], row["kind"], row["size"], row["act_class"], row["human_owner"], row["thread"], row["found"]),
                         ("rules crash on quantity zero", "bug", "S", "R", "tester", thread, True))
        self.assertEqual(row["paths"], ["fxapp/main.py", "fxapp/rules.py", "tests/test_rules_request.py"])
        self.assertEqual(row["commit"], git(self.project, "rev-parse", row["target_ref"]))
        self.assertGreater(row["diff_bytes"], 0)
        self.assertEqual([(layer["component"], layer["paths"]) for layer in row["layers"]],
                         [("rules", ["fxapp/rules.py"]), ("main", ["fxapp/main.py"]), (None, ["tests/test_rules_request.py"])])
        shown = review_queue.text([row])
        for part in ("1. github_issue:acme/app#1  rules crash on quantity zero", "bug S  class R  owner tester",
                     "placed by: blocks 0, 3 file(s),", f"at {row['commit'][:10]}; accepted, not pushed or merged here",
                     "read in 3 layer(s), each after what it depends on:",
                     "     1. rules: fxapp/rules.py", "     3. (no component): tests/test_rules_request.py",
                     "1 change(s) ready. Nothing here pushes, splits or merges one."):
            self.assertIn(part, shown)

    def test_two_ready_changes_to_one_file_name_each_other(self):
        self.ready("acme/app#1", ["fxapp/rules.py"])
        self.ready("acme/app#2", ["fxapp/rules.py", "fxapp/effects.py"], by=OTHER)
        first, second = self.rows()
        self.assertEqual((first["overlaps"], second["overlaps"]), ([second["ticket_id"]], [first["ticket_id"]]))
        self.assertIn("whichever merges second is rebased", review_queue.text([first, second]))

    def test_the_paths_are_the_accepted_commit_s_not_the_plan_s(self):
        self.planned_by_hand("acme/app#1", ["fxapp/rules.py", "fxapp/effects.py"])
        work.claim(self.home, "acme/app#1", AGENT)
        found = work.start(self.home, "acme/app#1", AGENT, repo_root=self.project, kit_home=".syberlabs", checks=CHECKS)
        kit = Kit.local(self.project / ".syberlabs", self.project)
        try:
            thread = kit.open(found["thread"])
            rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
            [candidate] = thread.propose(PatchProvider({"fxapp/rules.py": rules + "\n# only this one\n"}))
            thread.check(candidate.id)
            thread.accept(candidate.id)
        finally:
            kit.close()
        work.settle(self.home, "acme/app#1", repo_root=self.project, kit_home=".syberlabs")
        [row] = self.rows()
        self.assertEqual(row["paths"], ["fxapp/rules.py"], "the plan allowed two files; the accepted change touched one")

    def test_a_commit_this_repository_does_not_have_keeps_its_row_and_goes_last(self):
        self.ready("acme/app#1", ["fxapp/rules.py", "fxapp/effects.py"])
        self.ready("acme/app#4", ["fxapp/main.py"])
        here = {row["source"]["ref"]: row for row in self.rows()}
        self.assertEqual(list(here), ["acme/app#4", "acme/app#1"], "with both commits at hand, the one file is read first")
        clone = self.scratch / "clone"  # another clone with the ledger: main and one of the two accepted branches
        git(self.scratch, "clone", "-q", "--no-local", "--single-branch", "--branch", "main", str(self.project), str(clone))
        kept = here["acme/app#1"]["target_ref"]
        git(clone, "fetch", "-q", "origin", f"{kept}:{kept}")
        rows = review_queue.queue(self.home, repo_root=clone)
        self.assertEqual([(row["source"]["ref"], row["found"], row["diff_bytes"] is None) for row in rows],
                         [("acme/app#1", True, False), ("acme/app#4", False, True)], "a change that cannot be read is not read first")
        self.assertEqual(rows[1]["paths"], ["fxapp/main.py"], "the plan's paths stand in")
        self.assertIn("that commit is not in this repository", review_queue.text(rows))

    def test_a_long_title_is_one_short_line_and_a_lost_snapshot_costs_only_the_title(self):
        title = "rules crash on quantity zero " + "when the order path runs again " * 6
        self.triaged("acme/app#1", title, REQUESTS["acme/app#1"][1])
        work.make_plan(self.home, "acme/app#1", repo_root=self.project, written=(HUMAN, plan_of(["fxapp/rules.py"])))
        work.claim(self.home, "acme/app#1", AGENT)
        found = work.start(self.home, "acme/app#1", AGENT, repo_root=self.project, kit_home=".syberlabs", checks=CHECKS)
        kit = Kit.local(self.project / ".syberlabs", self.project)
        try:
            thread = kit.open(found["thread"])
            rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
            [candidate] = thread.propose(PatchProvider({"fxapp/rules.py": rules + "\n# guarded\n"}))
            thread.check(candidate.id)
            thread.accept(candidate.id)
        finally:
            kit.close()
        work.settle(self.home, "acme/app#1", repo_root=self.project, kit_home=".syberlabs")
        [row] = self.rows()
        self.assertEqual((len(row["title"]), row["title"]), (120, " ".join(title.split())[:120]))
        for snapshot in (self.scratch / "ledger" / intake.ARTIFACTS).glob("*.json"):
            snapshot.unlink()
        [row] = self.rows()
        self.assertEqual((row["title"], row["paths"]), ("", ["fxapp/rules.py"]))
        self.assertIn("(no title)", review_queue.text([row]))

    def test_a_ready_record_without_its_start_record_still_has_a_row(self):
        """A ledger someone appended to by hand: the queue lists what it can and reads no base it was not given."""
        self.planned_by_hand("acme/app#1", ["fxapp/rules.py"])
        current = work.state(self.home, "acme/app#1")
        self.home.append("note", {"note": "work_ready", "ticket_id": current["ticket_id"], "plan_digest": current["plan"]["plan_digest"],
                                  "refs": {"thread": "t-by-hand", "candidate": "c1", "commit": self.base, "target_ref": "refs/heads/by-hand"}})
        [row] = self.rows()
        self.assertEqual((row["found"], row["paths"], row["act_class"], row["diff_bytes"]), (False, ["fxapp/rules.py"], None, None))

    def test_settling_everything_finds_the_accepted_threads_and_leaves_the_rest(self):
        self.planned_by_hand("acme/app#1", ["fxapp/rules.py"])
        self.planned_by_hand("acme/app#2", ["fxapp/effects.py"])
        self.planned_by_hand("acme/app#3", ["fxapp/ingest.py"])
        threads = {}
        for ref, by in (("acme/app#1", AGENT), ("acme/app#2", OTHER)):
            work.claim(self.home, ref, by)
            threads[ref] = work.start(self.home, ref, by, repo_root=self.project, kit_home=".syberlabs", checks=CHECKS)["thread"]
        kit = Kit.local(self.project / ".syberlabs", self.project)
        try:
            thread = kit.open(threads["acme/app#1"])
            rules = (self.project / "fxapp" / "rules.py").read_text(encoding="utf-8")
            [candidate] = thread.propose(PatchProvider({"fxapp/rules.py": rules + "\n# guarded\n"}))
            thread.check(candidate.id)
            thread.accept(candidate.id)
        finally:
            kit.close()
        self.assertEqual(self.rows(), [], "accepted on its thread, and the ledger has not been told")
        later = work.state(self.home, "acme/app#2")["claim"]["until"] + 1  # the newer claim's end: both have lapsed by then
        looked = work.settle_all(self.home, repo_root=self.project, kit_home=".syberlabs", now=later)
        self.assertEqual([(row["source"]["ref"], row["status"]) for row in looked], [("acme/app#1", "ready"), ("acme/app#2", "planned")],
                         "the ticket with no thread is not looked at; a lapsed claim does not hide an accepted change")
        self.assertEqual([row["source"]["ref"] for row in self.rows()], ["acme/app#1"])
        self.assertEqual(len(work.settle_all(self.home, repo_root=self.project, kit_home=".syberlabs")), 1, "only the one still without a record")
        self.assertEqual(len(self.notes("work_ready")), 1)

    def test_reading_the_queue_writes_nothing(self):
        self.ready("acme/app#1", ["fxapp/rules.py"])
        before = (len(self.home.events()), git(self.project, "for-each-ref"), git(self.project, "status", "--porcelain"))
        self.rows()
        review_queue.text(self.rows())
        self.assertEqual((len(self.home.events()), git(self.project, "for-each-ref"), git(self.project, "status", "--porcelain")), before)
        self.assertTrue(self.home.verify())

    def test_a_declined_request_leaves_the_queue(self):
        self.ready("acme/app#1", ["fxapp/rules.py"])
        intake.resolve(self.home, "acme/app#1", "declined", HUMAN, reason="not now")
        self.assertEqual(self.rows(), [])


class Cli(Queue):
    def run_cli(self, *argv, code=0) -> str:
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.scratch)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                found = factory_cli.main(["--home", str(self.scratch / "ledger"), "--root", str(self.project), *argv])
        finally:
            os.chdir(here)
        self.assertEqual(found, code, err.getvalue() + out.getvalue())
        return out.getvalue()

    def test_the_queue_from_a_terminal(self):
        self.assertIn("nothing is ready", self.run_cli("work", "queue"))
        self.assertIn("0 of 0 thread(s) hold an accepted change", self.run_cli("work", "settle"))
        self.ready("acme/app#1", ["fxapp/rules.py"], ["tests/test_rules_request.py"])
        self.planned_by_hand("acme/app#2", ["fxapp/effects.py"])
        work.claim(self.home, "acme/app#2", OTHER)
        work.start(self.home, "acme/app#2", OTHER, repo_root=self.project, kit_home=".syberlabs", checks=CHECKS)
        settled = self.run_cli("work", "settle")
        self.assertIn("working", settled)
        self.assertIn("0 of 1 thread(s) hold an accepted change", settled, "the ready ticket already has its record")
        shown = self.run_cli("work", "queue")
        self.assertIn("1. github_issue:acme/app#1  rules crash on quantity zero", shown)
        self.assertIn("read in 2 layer(s), each after what it depends on:", shown)
        [row] = json.loads(self.run_cli("work", "queue", "--json"))
        self.assertEqual((row["position"], row["source"]["ref"], [layer["component"] for layer in row["layers"]]), (1, "acme/app#1", ["rules", None]))


if __name__ == "__main__":
    unittest.main()
