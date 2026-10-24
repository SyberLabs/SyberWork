"""The settlement poll: merges, closes and reviews of opened pull requests become verified host observations."""

import contextlib
import io
import json
import os
import re
import subprocess
import threading
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from spec.validate import validate_events
from syberlabs import metrics, settle
from syberlabs.build import Runner
from syberlabs.clock import as_seconds
from syberlabs.cli import main as cli
from tests.test_build_thread import GOOD, RepoCase, default_contract, git

TOKEN = "settle-test-token-7f3a9c41"
TOKEN_ENV = "TEST_SETTLE_TOKEN"
FACTS = [{"key": "accepted", "source": "git", "verified": True}]
BOUND = {"commit": "fact:accepted.commit"}


def remote_head(bare, ref):
    found = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet", ref],
                           capture_output=True, text=True)
    return found.stdout.strip() or None


class FakeGitHub:
    """The pulls API a publish and a settlement pass use: list by head, create, read one, list its reviews."""

    def __init__(self, bare):
        self.bare, self.pulls, self.reviews, self.mode, self.requests = bare, {}, {}, "ok", []
        self.raw = None             # (status, headers, body bytes) served for a pull request read, when set
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                url = urlsplit(self.path)
                owner.requests.append(url.path)
                if self.headers["Authorization"] != "Bearer " + TOKEN:
                    return self.reply(401, {"message": "Bad credentials"})
                one = re.fullmatch(r"/repos/acme/shop/pulls/(\d+)(/reviews)?", url.path)
                if one is None:
                    head = parse_qs(url.query)["head"][0].split(":", 1)[1]
                    return self.reply(200, [p for p in owner.pulls.values() if p["head"]["ref"] == head])
                if owner.mode == "error":
                    return self.reply(500, {"message": "Server Error"})
                number = int(one.group(1))
                if number not in owner.pulls:
                    return self.reply(404, {"message": "Not Found"})
                if owner.raw is not None and not one.group(2):
                    status, headers, raw = owner.raw
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    return self.wfile.write(raw)
                if one.group(2):
                    page = int(parse_qs(url.query).get("page", ["1"])[0])
                    return self.reply(200, owner.reviews.get(number, []) if page == 1 else [])
                return self.reply(200, owner.pulls[number])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sha = remote_head(owner.bare, "refs/heads/" + body["head"])
                number = len(owner.pulls) + 1
                pull = {"number": number, "html_url": f"https://github.test/acme/shop/pull/{number}", "state": "open",
                        "merged": False, "merged_at": None, "merge_commit_sha": None, "merged_by": None,
                        "closed_at": None, "head": {"ref": body["head"], "sha": sha}, "base": {"ref": body["base"]},
                        "title": body["title"], "body": body["body"]}
                owner.pulls[number] = pull
                self.reply(201, pull)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def review(self, number, review_id, state, login, at):
        self.reviews.setdefault(number, []).append(
            {"id": review_id, "user": {"login": login}, "state": state, "submitted_at": at, "body": "looks fine"})

    def merge(self, number, at, by="carol"):
        self.pulls[number].update({"state": "closed", "merged": True, "merged_at": at, "closed_at": at,
                                   "merge_commit_sha": "a" * 40, "merged_by": {"login": by}})

    def close(self, number=None, at=None):
        if number is None:
            self.server.shutdown()
            self.server.server_close()
            return
        self.pulls[number].update({"state": "closed", "closed_at": at})


class SettleCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.bare = self.root.parent / "remote.git"
        git(self.root.parent, "init", "-q", "--bare", str(self.bare))
        git(self.root, "remote", "add", "origin", str(self.bare))
        git(self.root, "push", "-q", "origin", "main")
        self.github = FakeGitHub(self.bare)
        self.addCleanup(self.github.close)
        os.environ[TOKEN_ENV] = TOKEN
        self.addCleanup(os.environ.pop, TOKEN_ENV, None)

    def published(self, objective="Add a CSV export", kit=None):
        """A thread accepted, pushed and with its pull request opened on the fake GitHub."""
        if kit is None:
            doc = default_contract(self.root)
            doc["actions"].update({
                "push_branch": {"requires_effect": "accept_change", "required_facts": FACTS, "arguments": BOUND},
                "open_pull_request": {"requires_effect": "push_branch", "required_facts": FACTS, "arguments": BOUND},
            })
            policy = {"version": 1, "actions": {name: {"roles": ["developer"]} for name in doc["actions"]}}
            home = self.root / ".syberlabs"
            home.mkdir(exist_ok=True)
            (home / "actions.json").write_text(json.dumps({
                "push_branch": {"kind": "local", "effect": "git_push", "remote": "origin", "settle_seconds": 0},
                "open_pull_request": {"kind": "local", "effect": "github_pull_request", "settle_seconds": 0,
                                      "api": f"http://127.0.0.1:{self.github.server.server_port}",
                                      "repository": "acme/shop", "base": "main", "token_env": TOKEN_ENV}}))
            kit = self.kit(doc, policy)
        thread = kit.start(objective, paths=["src/", "tests/"], unattended=True)
        thread.propose(changes=GOOD)
        thread.check("c1")
        self.assertEqual(thread.accept("c1").status, "succeeded")
        self.assertEqual(thread.publish("push_branch").status, "succeeded")
        opened = thread.publish("open_pull_request")
        self.assertEqual(opened.status, "succeeded")
        return kit, thread, int(opened.external_id)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli(["--repo", str(self.root), "--home", str(self.root / ".syberlabs"),
                        "--actor", "dev@example.test", *args])
        return code, out.getvalue(), err.getvalue()

    def facts(self, thread, key):
        return [e for e in thread.history() if e["kind"] == "observed" and e["body"]["key"] == key]

    def item(self, kit, thread):
        [found] = [i for i in metrics.work_items(thread.history(), dict(kit.session.contracts)) if i.thread == thread.id]
        return found

    def at(self, thread, seconds_after):
        created = next(e for e in thread.history() if e["kind"] == "case_created")
        return metrics.utc(float(int(as_seconds(created["at"])) + seconds_after))

    def assertSound(self, thread, label):
        self.assertTrue(thread.kit.session.verify_chain(thread.id), label)
        self.assertEqual(validate_events(thread.history(), label), [], label)


class Poll(SettleCase):
    def test_reviews_and_the_merge_are_recorded_once_and_fold_into_delivered_and_agent_time(self):
        kit, thread, number = self.published()
        self.assertEqual(settle.published(kit, thread.id)[0].branch, f"syberlabs/{thread.id[:8]}")
        [first] = settle.settle(kit)
        self.assertEqual((first.thread, first.number, first.state, first.recorded, first.error),
                         (thread.id, number, "open", [], None))
        self.assertIsNone(self.item(kit, thread).delivered.value, "open and unmerged is unknown, not false")

        self.github.review(number, 11, "CHANGES_REQUESTED", "alice", self.at(thread, 600))
        self.github.review(number, 12, "APPROVED", "bob", self.at(thread, 3600))
        self.github.review(number, 13, "PENDING", "dave", None)
        [second] = settle.settle(kit)
        self.assertEqual(second.recorded, ["pr_review 11 changes_requested", "pr_review 12 approved"])
        self.assertEqual(settle.settle(kit)[0].recorded, [], "a review id is recorded once")
        reviews = self.facts(thread, "pr_review")
        self.assertEqual([e["body"]["value"] for e in reviews], [
            {"number": number, "review_id": 11, "state": "changes_requested", "reviewer": "alice", "at": self.at(thread, 600)},
            {"number": number, "review_id": 12, "state": "approved", "reviewer": "bob", "at": self.at(thread, 3600)}])
        self.assertEqual([(e["body"]["actor"], e["body"]["verified"], e["body"]["source"], e["body"]["version"])
                          for e in reviews], [("syberlabs.host", True, "github:acme/shop", "11"),
                                              ("syberlabs.host", True, "github:acme/shop", "12")])
        self.assertEqual(self.item(kit, thread).review_rejections.value, 1, "changes requested is a rejection")

        merged_at = self.at(thread, 7200)
        self.github.merge(number, merged_at)
        [third] = settle.settle(kit)
        self.assertEqual((third.state, third.recorded), ("merged", [f"pr_merged {merged_at}"]))
        [merged] = self.facts(thread, "pr_merged")
        self.assertEqual(merged["body"]["value"],
                         {"number": number, "merged_at": merged_at, "merge_commit": "a" * 40, "merged_by": "carol"})
        self.assertEqual((merged["body"]["actor"], merged["body"]["verified"], merged["body"]["version"]),
                         ("syberlabs.host", True, "a" * 40))
        asked = len(self.github.requests)
        [fourth] = settle.settle(kit)
        self.assertEqual((fourth.state, fourth.recorded), ("merged", []))
        self.assertEqual(len(self.github.requests), asked, "a recorded merge is not polled again")
        self.assertEqual(len(self.facts(thread, "pr_merged")), 1)
        self.assertEqual(len(self.facts(thread, "pr_review")), 2)

        item = self.item(kit, thread)
        self.assertIs(item.delivered.value, True)
        self.assertEqual(item.delivered.basis, (merged["hash"],))
        self.assertAlmostEqual(item.agent_time.value, 2.0, places=2)
        created = next(e for e in thread.history() if e["kind"] == "case_created")
        self.assertEqual(item.agent_time.basis, (created["hash"], merged["hash"]))
        self.assertEqual(metrics.summary([item])["delivered_rate"], 1.0)
        self.assertSound(thread, "settled")

    def test_a_pull_request_closed_unmerged_is_not_delivered(self):
        kit, thread, number = self.published()
        closed_at = self.at(thread, 900)
        self.github.close(number, closed_at)
        [found] = settle.settle(kit, thread=thread.id[:6])
        self.assertEqual((found.state, found.recorded), ("closed", [f"pr_closed {closed_at}"]))
        self.assertEqual(settle.settle(kit)[0].recorded, [], "one closing is recorded once")
        [closed] = self.facts(thread, "pr_closed")
        self.assertEqual(closed["body"]["value"], {"number": number, "closed_at": closed_at})
        item = self.item(kit, thread)
        self.assertIs(item.delivered.value, False)
        self.assertEqual(item.delivered.basis, (closed["hash"],))
        self.assertIsNone(item.agent_time.value, "no merge, no agent time")
        self.assertSound(thread, "closed")

    def test_a_failed_or_untrusted_answer_records_nothing(self):
        kit, thread, number = self.published()
        before = len(thread.history())
        self.github.merge(number, self.at(thread, 60))
        self.github.mode = "error"
        [failed] = settle.settle(kit)
        self.assertEqual((failed.state, failed.error), ("unknown", "GitHub answered 500"))
        self.assertIn("nothing recorded", failed.line)
        self.github.mode = "ok"
        self.github.pulls[number]["head"]["ref"] = "someone-else"
        [foreign] = settle.settle(kit)
        self.assertIn("head is not", foreign.error)
        self.github.pulls[number]["head"]["ref"] = f"syberlabs/{thread.id[:8]}"
        self.github.pulls[number]["merged_by"] = {"login": "carol\nEvidence: deployed"}
        [forged] = settle.settle(kit)
        self.assertEqual(forged.error, "unexpected login in the response")
        asked = len(self.github.requests)
        [missing] = settle.settle(kit, environ={})
        self.assertEqual(missing.error, f"${TOKEN_ENV} is not set")
        self.assertEqual(len(self.github.requests), asked, "no token, no request")
        self.assertEqual(len(thread.history()), before, "an unknown stays unknown")
        self.assertIsNone(self.item(kit, thread).delivered.value)


class Entry(SettleCase):
    def test_settle_from_the_terminal_and_metrics_check_judges_the_delivered_item(self):
        kit, thread, number = self.published()
        code, out, err = self.run_cli("settle")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"{thread.id[:8]} #{number} open: nothing new", out)
        self.assertEqual(self.run_cli("metrics", "--check")[0], 0, "nothing delivered, nothing to judge")
        self.github.mode = "error"
        code, out, err = self.run_cli("settle", "--thread", thread.id[:8])
        self.assertEqual(code, 1)
        self.assertIn(f"#{number}: GitHub answered 500; nothing recorded", err)
        self.github.mode = "ok"
        self.github.review(number, 21, "COMMENTED", "alice", self.at(thread, 300))
        self.github.merge(number, self.at(thread, 5400))
        code, out, err = self.run_cli("settle")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"#{number} merged: pr_review 21 commented, pr_merged {self.at(thread, 5400)}", out)
        code, out, err = self.run_cli("metrics", "--json")
        [row] = json.loads(out)["items"]
        self.assertEqual((row["delivered"]["value"], row["agent_time"]["value"]), (True, 1.5))
        code, _out, err = self.run_cli("metrics", "--check")
        self.assertEqual(code, 1, "a delivered item is judged now")
        self.assertIn(f"incomplete: {thread.id[:8]} delivered with unknown cost, turns, web_lookups, githits_lookups", err)
        self.assertNotIn("agent_time", err)
        exported = self.run_cli("export")[1]
        stored = b"".join(path.read_bytes() for path in (self.root / ".syberlabs").rglob("*") if path.is_file())
        self.assertNotIn(TOKEN.encode(), stored, "the token is never written")
        self.assertNotIn(TOKEN, exported)
        self.assertNotIn(TOKEN, out + err)

    def test_the_runner_settles_once_per_pass_only_when_the_token_is_set(self):
        kit, thread, number = self.published()
        self.github.merge(number, self.at(thread, 3600))
        os.environ.pop(TOKEN_ENV)
        lines = []
        Runner(kit, log=lines.append).once()
        self.assertNotIn(f"/repos/acme/shop/pulls/{number}", self.github.requests)
        self.assertEqual(self.facts(thread, "pr_merged"), [])
        os.environ[TOKEN_ENV] = TOKEN
        steps = Runner(kit, log=lines.append).once()
        self.assertFalse(any(step.taken for step in steps), "settling is not a step")
        self.assertIn(f"{thread.id[:8]} #{number} merged: pr_merged {self.at(thread, 3600)}", lines)
        self.assertEqual(len(self.facts(thread, "pr_merged")), 1)
        self.assertNotIn(TOKEN, "\n".join(lines))
        self.assertSound(thread, "runner settled")


class Hardened(SettleCase):
    """What GitHub answers is untrusted: a malformed, redirected or oversized answer records nothing and never
    changes what ``syberlabs run`` does."""

    def settled_thread(self):
        kit, thread, number = self.published()
        self.github.merge(number, self.at(thread, 60))
        self.github.review(number, 31, "APPROVED", "bob", self.at(thread, 30))
        return kit, thread, number

    def assert_refused(self, kit, thread, why, before):
        [found] = settle.settle(kit)
        self.assertEqual((found.state, found.recorded), ("unknown", []), why)
        self.assertIsNotNone(found.error, why)
        self.assertEqual(len(thread.history()), before, why + ": nothing recorded")
        self.assertIsNone(self.item(kit, thread).delivered.value, why)

    def baseline(self, kit, thread):
        """What a pass does with the settlement poll off (no token): the outcome every guarded pass must match."""
        os.environ.pop(TOKEN_ENV)
        try:
            code, out, _err = self.run_cli("run", "--once", "--thread", thread.id[:8])
            steps = list(Runner(kit, log=lambda line: None).once())
        finally:
            os.environ[TOKEN_ENV] = TOKEN
        return code, out, steps

    def test_malformed_answers_record_nothing_and_leave_run_unchanged(self):
        kit, thread, number = self.settled_thread()
        before = len(thread.history())
        base_code, base_out, base_steps = self.baseline(kit, thread)
        good = self.github.pulls[number]
        review = self.github.reviews[number][0]
        shapes = {
            "head is a list": lambda: good.update({"head": ["x"]}),
            "head is a string": lambda: good.update({"head": "x"}),
            "head.ref is a number": lambda: good.update({"head": {"ref": 5}}),
            "number is true": lambda: good.update({"number": True}),
            "number is a string": lambda: good.update({"number": str(number)}),
            "merged is 1": lambda: good.update({"merged": 1}),
            "merged_at is an int": lambda: good.update({"merged_at": 1700000000}),
            "merge_commit_sha is a number": lambda: good.update({"merge_commit_sha": 7}),
            "merged_by is a string": lambda: good.update({"merged_by": "carol"}),
            "state is a list": lambda: good.update({"state": ["closed"]}),
            "review user is a string": lambda: review.update({"user": "bob"}),
            "review user is a list": lambda: review.update({"user": ["bob"]}),
            "review state is a number": lambda: review.update({"state": 5}),
            "review id is true": lambda: review.update({"id": True}),
        }
        saved, saved_review = json.loads(json.dumps(good)), json.loads(json.dumps(review))
        for why, break_it in shapes.items():
            with self.subTest(why):
                good.clear(), good.update(json.loads(json.dumps(saved)))
                review.clear(), review.update(json.loads(json.dumps(saved_review)))
                break_it()
                self.assert_refused(kit, thread, why, before)
                code, out, err = self.run_cli("run", "--once", "--thread", thread.id[:8])
                self.assertEqual((code, out), (base_code, base_out), why + ": run's outcome is unchanged")
                self.assertIn("nothing recorded", err, why)
                self.assertNotIn("Traceback", err, why)
                steps = list(Runner(kit, log=lambda line: None).once())
                self.assertEqual(steps, base_steps, why + ": the steps are unchanged")
                self.assertNotIn(TOKEN, out + err)
        good.clear(), good.update(saved)
        review.clear(), review.update(saved_review)
        [found] = settle.settle(kit)
        self.assertEqual(found.state, "merged", "the same thread settles once the answer is well formed")
        self.assertSound(thread, "after malformed answers")

    def test_a_redirect_and_an_oversized_body_are_refused(self):
        kit, thread, number = self.settled_thread()
        before = len(thread.history())
        self.github.raw = (302, {"Location": f"/repos/acme/shop/pulls/{number}"}, b"")
        self.assert_refused(kit, thread, "redirect", before)
        self.assertEqual(settle.settle(kit)[0].error, "GitHub answered 302")
        self.github.raw = (301, {"Location": "http://127.0.0.1:1/elsewhere"}, b"{}")
        self.assertEqual(settle.settle(kit)[0].error, "GitHub answered 301")
        pad = b" " * (settle.MAX_BYTES + 1)
        self.github.raw = (200, {}, json.dumps(self.github.pulls[number]).encode() + pad)
        self.assert_refused(kit, thread, "oversize", before)
        self.assertEqual(settle.settle(kit)[0].error, "response too large")
        self.github.raw = (200, {}, b"[" * 200000)
        self.assertEqual(settle.settle(kit)[0].error, "unreadable response", "deep nesting is unreadable, not a crash")
        self.github.raw = None
        self.assertEqual(settle.settle(kit)[0].state, "merged")

    def test_an_unexpected_error_in_the_pass_never_changes_run(self):
        kit, thread, number = self.settled_thread()
        base_code, base_out, base_steps = self.baseline(kit, thread)
        boom = KeyError(f"api {TOKEN}\nsecond line")
        with mock.patch.object(settle, "settle", side_effect=boom):
            code, out, err = self.run_cli("run", "--once", "--thread", thread.id[:8])
            self.assertEqual((code, out), (base_code, base_out))
            self.assertEqual(err.count("\n"), 1, "one line on stderr")
            self.assertTrue(err.startswith("settle: KeyError: "), err)
            self.assertNotIn(TOKEN, err)
            self.assertNotIn("Traceback", err)
            steps = list(Runner(kit, log=lambda line: None).once())
            self.assertEqual(steps, base_steps)
            code, out, err = self.run_cli("settle")
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.startswith("settle: KeyError: ") and err.count("\n") == 1, err)
            self.assertNotIn(TOKEN, err)
        with mock.patch.object(settle, "tokens_available", side_effect=RuntimeError("journal")):
            code, out, err = self.run_cli("run", "--once", "--thread", thread.id[:8])
            self.assertEqual((code, out, err), (base_code, base_out, "settle: RuntimeError: journal\n"))
        stored = b"".join(path.read_bytes() for path in (self.root / ".syberlabs").rglob("*") if path.is_file())
        self.assertNotIn(TOKEN.encode(), stored, "the token is never written")
        self.assertSound(thread, "after an unexpected error")


if __name__ == "__main__":
    import unittest
    unittest.main()
