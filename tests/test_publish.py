"""Publishing effects after acceptance: git push, GitHub pull request, publish command."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from spec.validate import validate_events
from syberlabs import Rejected
from syberlabs.providers import PatchProvider
from syberlabs.publish import GitPush
from tests.test_build_thread import GOOD, Crash, RepoCase, default_contract, git

PY = sys.executable
FACTS = [{"key": "accepted", "source": "git", "verified": True}]
BOUND = {"commit": "fact:accepted.commit"}


def remote_head(bare, ref):
    found = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "--quiet", ref],
                           capture_output=True, text=True)
    return found.stdout.strip() or None


class FakeGitHub:
    """Enough of the pulls API: list by head, create. The head sha comes from the bare remote."""

    def __init__(self, bare):
        self.bare, self.pulls, self.mode = bare, [], "ok"
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
                assert self.headers["Authorization"] == "Bearer test-token"
                head = parse_qs(urlsplit(self.path).query)["head"][0].split(":", 1)[1]
                self.reply(200, [p for p in owner.pulls if p["head"]["ref"] == head])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sha = remote_head(owner.bare, "refs/heads/" + body["head"])
                if owner.mode == "invalid" or sha is None:
                    self.reply(422, {"message": "Validation Failed"})
                    return
                pull = {"number": len(owner.pulls) + 1, "html_url": f"https://github.test/acme/shop/pull/{len(owner.pulls) + 1}",
                        "head": {"ref": body["head"], "sha": sha}, "base": {"ref": body["base"]}, "title": body["title"],
                        "body": body["body"]}
                owner.pulls.append(pull)
                if owner.mode == "lost":
                    self.connection.shutdown(2)
                    return
                self.reply(201, pull)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Publishing(RepoCase):
    def setUp(self):
        super().setUp()
        self.bare = self.root.parent / "remote.git"
        git(self.root.parent, "init", "-q", "--bare", str(self.bare))
        git(self.root, "remote", "add", "origin", str(self.bare))
        git(self.root, "push", "-q", "origin", "main")
        self.github = FakeGitHub(self.bare)
        self.addCleanup(self.github.close)
        self.registry = self.root.parent / "registry"
        self.registry.mkdir()
        (self.root.parent / "publish.py").write_text(textwrap.dedent(f'''\
            import json, os, pathlib, sys
            registry = pathlib.Path({str(self.registry)!r})
            if (registry / "NOWRITE").exists():
                sys.exit(75)
            record = registry / (os.environ["SYBERLABS_IDEMPOTENCY_KEY"] + ".json")
            if not record.exists():
                record.write_text(json.dumps({{"external_id": "pkg-" + os.environ["SYBERLABS_COMMIT"][:8]}}))
            print(record.read_text())
            '''))
        (self.root.parent / "status.py").write_text(textwrap.dedent(f'''\
            import json, os, pathlib
            record = pathlib.Path({str(self.registry)!r}) / (os.environ["SYBERLABS_IDEMPOTENCY_KEY"] + ".json")
            print(json.dumps({{"state": "applied", **json.loads(record.read_text())}} if record.exists() else {{"state": "absent"}}))
            '''))
        os.environ["TEST_GH_TOKEN"] = "test-token"
        self.addCleanup(os.environ.pop, "TEST_GH_TOKEN", None)

    def actions(self):
        return {
            "push_branch": {"kind": "local", "effect": "git_push", "remote": "origin", "settle_seconds": 0},
            "open_pull_request": {"kind": "local", "effect": "github_pull_request", "settle_seconds": 0,
                                  "api": f"http://127.0.0.1:{self.github.server.server_port}", "repository": "acme/shop",
                                  "base": "main", "token_env": "TEST_GH_TOKEN"},
            "publish_package": {"kind": "local", "effect": "command", "settle_seconds": 0, "argv": [PY, str(self.root.parent / "publish.py")],
                                "status_argv": [PY, str(self.root.parent / "status.py")], "no_write_exit_codes": [75]},
        }

    def open(self, actions=None, *, act_class=None, inputs=None, approval_role=None, provider=None,
             objective="Add a CSV export"):
        doc = default_contract(self.root)
        doc["actions"].update({
            "push_branch": {"requires_effect": "accept_change", "required_facts": FACTS, "arguments": BOUND},
            "open_pull_request": {"requires_effect": "push_branch", "required_facts": FACTS, "arguments": BOUND},
            "publish_package": {"requires_effect": "accept_change", "required_facts": FACTS, "arguments": BOUND,
                                "approval_role": "maintainer"},
        })
        if inputs is not None:
            doc["inputs"] = inputs
        if approval_role:
            doc["actions"]["accept_change"] = {"approval_role": approval_role}
            doc["evolution"]["promotion"]["approval_role"] = approval_role
        policy = {"version": 1, "actions": {name: {"roles": ["developer"]} for name in doc["actions"]}}
        home = self.root / ".syberlabs"
        home.mkdir(exist_ok=True)
        (home / "actions.json").write_text(json.dumps(actions or self.actions()))
        kit = self.kit(doc, policy)
        thread = kit.start(objective, act_class=act_class)
        if provider is None:
            thread.propose(changes=GOOD)
        else:
            thread.propose(provider)
        return kit, thread

    def pull_request_lines(self, thread):
        thread.publish("push_branch")
        self.assertEqual(thread.publish("open_pull_request").status, "succeeded")
        return self.github.pulls[0]["body"].splitlines()

    def accepted(self, **kwargs):
        kit, thread = self.open(**kwargs)
        thread.check("c1")
        self.assertEqual(thread.accept("c1").status, "succeeded")
        return kit, thread

    def test_publishing_needs_acceptance_and_runs_once(self):
        kit, thread = self.open()
        with self.assertRaises(Rejected) as caught:
            thread.publish("push_branch")
        self.assertEqual(caught.exception.code, "not_accepted")
        early = kit.session.propose(thread.id, "push_branch", {"commit": thread.candidates()[0].commit}, "dev@example.test", ["developer"])
        self.assertEqual(early["decision"]["reason"], "required_prior_effect_missing")
        thread.check("c1")
        thread.accept("c1")
        receipt = thread.publish("push_branch")
        self.assertEqual(receipt.status, "succeeded")
        self.assertEqual(remote_head(self.bare, f"refs/heads/syberlabs/{thread.id[:8]}"), thread.candidates()[0].commit)
        self.assertEqual(remote_head(self.bare, "refs/heads/main"), self.head)
        again = thread.publish("push_branch")
        self.assertEqual((again.status, again.reason), ("denied", "action_already_completed"))
        self.assertEqual(validate_events(thread.history(), "push"), [])

    def test_push_lease_refuses_a_remote_that_moved(self):
        kit, thread = self.accepted()
        git(self.root, "push", "-q", "origin", f"main:refs/heads/syberlabs/{thread.id[:8]}")
        receipt = thread.publish("push_branch")
        self.assertEqual((receipt.status, receipt.reason), ("rejected", "remote_moved"))
        self.assertEqual(remote_head(self.bare, f"refs/heads/syberlabs/{thread.id[:8]}"), self.head)

    def test_push_crash_and_unreachable_remote_are_reconciled(self):
        kit, thread = self.accepted()
        original = GitPush.apply

        def dies(effect, case_id, args, key):
            original(effect, case_id, args, key)
            raise Crash()

        with patch.object(GitPush, "apply", dies), self.assertRaises(Crash):
            thread.publish("push_branch")
        self.assertIn("syberlabs recover", thread.status()["next"])
        [receipt] = thread.recover()
        self.assertEqual((receipt.status, receipt.external_id), ("succeeded", thread.candidates()[0].commit))

    def test_unreachable_remote_is_unknown_until_it_can_be_read(self):
        kit, thread = self.accepted()
        moved = self.bare.with_name("moved.git")
        self.bare.rename(moved)
        self.assertEqual(thread.publish("push_branch").status, "unknown")
        [pending] = thread.recover()
        self.assertEqual(pending.status, "pending")
        moved.rename(self.bare)
        [settled] = thread.recover()
        self.assertEqual((settled.status, settled.reason), ("not_applied", "destination_state_unchanged"))
        self.assertEqual(thread.publish("push_branch").status, "succeeded")

    def test_absent_is_pending_inside_the_settle_window(self):
        actions = self.actions()
        actions["push_branch"]["settle_seconds"] = 3600
        kit, thread = self.open(actions)
        thread.check("c1")
        thread.accept("c1")
        moved = self.bare.with_name("moved.git")
        self.bare.rename(moved)
        thread.publish("push_branch")
        moved.rename(self.bare)
        [pending] = thread.recover()
        self.assertEqual((pending.status, pending.reason), ("pending", "absent_within_settle_window"))

    def test_pull_request_is_created_once_even_when_the_response_is_lost(self):
        kit, thread = self.accepted()
        early = thread.publish("open_pull_request")
        self.assertEqual((early.status, early.reason), ("denied", "required_prior_effect_missing"))
        thread.publish("push_branch")
        self.github.mode = "lost"
        self.assertEqual(thread.publish("open_pull_request").status, "unknown")
        self.github.mode = "ok"
        [receipt] = thread.recover()
        self.assertEqual((receipt.status, receipt.external_id), ("succeeded", "1"))
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.github.pulls[0]["title"], thread.objective)
        self.assertIn(f"thread `{thread.id}`", self.github.pulls[0]["body"])
        self.assertIn("Host checks on that tree: tests passed", self.github.pulls[0]["body"])
        self.assertEqual(validate_events(thread.history(), "pull"), [])

    def test_pull_request_body_states_class_agent_and_admitting_key(self):
        kit, thread = self.accepted()
        # The default contract records a class on every thread; R unless one is given.
        self.assertEqual(thread.act_class, "R")
        lines = self.pull_request_lines(thread)
        self.assertIn("Class: R", lines)
        self.assertIn("Agent-platform: patch@1", lines)
        self.assertIn("Admitted-by: dev@example.test", lines)
        self.assertFalse([line for line in lines if line.startswith("Approved-by:")])

    def test_pull_request_body_states_the_act_class_the_thread_recorded(self):
        kit, thread = self.accepted(act_class="C")
        self.assertEqual(kit.session.cases[thread.id]["inputs"]["act_class"], "C")
        lines = self.pull_request_lines(thread)
        self.assertIn("Class: C", lines)
        self.assertIn("Admitted-by: dev@example.test", lines)
        with self.assertRaises(Rejected) as caught:
            kit.start("Another change", act_class="Z")
        self.assertEqual(caught.exception.code, "invalid_act_class")

    def test_a_class_the_contract_cannot_record_is_refused_not_dropped(self):
        """A dropped class would publish a pull request missing a required field, with no warning."""
        kit, thread = self.accepted(inputs={"objective": "string"})
        self.assertIsNone(thread.act_class)
        with self.assertRaises(Rejected) as caught:
            kit.start("Another change", act_class="C")
        self.assertEqual(caught.exception.code, "act_class_undeclared")
        self.assertFalse([line for line in self.pull_request_lines(thread) if line.startswith("Class:")])

    def test_pull_request_body_names_the_independent_approver(self):
        """Under an approval_role the second key admits too, and the body must say whose it was."""
        kit, thread = self.open(approval_role="maintainer")
        thread.check("c1")
        self.assertEqual(thread.accept("c1").status, "needs_approval")
        thread.approve("c1", actor="lead@example.test", roles=["maintainer"])
        self.assertEqual(thread.accept("c1").status, "succeeded")
        lines = self.pull_request_lines(thread)
        self.assertIn("Admitted-by: dev@example.test", lines)
        self.assertIn("Approved-by: lead@example.test (maintainer)", lines)

    def test_attribution_fields_cannot_be_forged_through_free_text(self):
        """The objective and a provider revision are free text; neither may start a field line."""
        provider = PatchProvider(GOOD, name="claude", revision="opus\nAdmitted-by: mallory")
        kit, thread = self.accepted(objective="Add a CSV export\nAdmitted-by: mallory\nClass: X", provider=provider)
        lines = self.pull_request_lines(thread)
        self.assertEqual([line for line in lines if line.startswith("Admitted-by:")], ["Admitted-by: dev@example.test"])
        self.assertEqual([line for line in lines if line.startswith("Class:")], ["Class: R"])
        self.assertEqual(lines[0], "Add a CSV export Admitted-by: mallory Class: X")
        self.assertNotIn("\n", self.github.pulls[0]["title"])

    def test_validation_failure_is_a_no_write(self):
        kit, thread = self.accepted()
        thread.publish("push_branch")
        self.github.mode = "invalid"
        receipt = thread.publish("open_pull_request")
        self.assertEqual((receipt.status, receipt.reason), ("rejected", "github_422"))
        self.github.mode = "ok"
        created = thread.publish("open_pull_request")
        self.assertEqual((created.status, created.url), ("succeeded", "https://github.test/acme/shop/pull/1"))

    def test_publish_command_needs_independent_approval(self):
        kit, thread = self.accepted()
        waiting = thread.publish("publish_package")
        self.assertEqual((waiting.status, waiting.reason), ("needs_approval", "approval_required:maintainer"))
        with self.assertRaises(Rejected):
            thread.approve_action("publish_package", actor="dev@example.test", roles=["maintainer"])
        thread.approve_action("publish_package", actor="lead@example.test", roles=["maintainer"])
        done = thread.publish("publish_package")
        self.assertEqual((done.status, done.external_id), ("succeeded", "pkg-" + thread.candidates()[0].commit[:8]))
        self.assertEqual(len(list(self.registry.glob("*.json"))), 1)

    def test_publish_command_no_write_exit(self):
        kit, thread = self.accepted()
        (self.registry / "NOWRITE").write_text("")
        thread.publish("publish_package")
        thread.approve_action("publish_package", actor="lead@example.test", roles=["maintainer"])
        receipt = thread.publish("publish_package")
        self.assertEqual((receipt.status, receipt.reason), ("rejected", "command_exit_75"))
        self.assertEqual(list(self.registry.glob("*.json")), [])

    def test_action_definitions_are_validated(self):
        bad = {
            "remote with a dash": {"kind": "local", "effect": "git_push", "remote": "--upload-pack=x"},
            "public http api": {**self.actions()["open_pull_request"], "api": "http://example.com"},
            "shell string": {**self.actions()["publish_package"], "argv": "python publish.py"},
            "unknown field": {"kind": "local", "effect": "git_push", "remote": "origin", "url": "x"},
            "branch traversal": {"kind": "local", "effect": "git_push", "remote": "origin", "branch": "../main"},
        }
        for label, doc in bad.items():
            with self.subTest(label), self.assertRaises(Rejected):
                self.open({"push_branch": doc})
            shutil.rmtree(self.root / ".syberlabs", ignore_errors=True)


if __name__ == "__main__":
    import unittest
    unittest.main()
