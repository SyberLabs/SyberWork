"""EvoGit's multi-host mode: hosts share populations through a Git remote; each judges migrants itself."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from examples.evolve import PRICING, RuleMutator, contract, git, make_repository
from spec.validate import validate_events
from syberlabs import Kit, Rejected
from syberlabs.evolve import EvolutionaryProvider
from syberlabs.providers import FunctionProvider
from tests.test_evolve import fix


def git_input(root, data, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.test",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "dev@example.test"}
    return subprocess.run(["git", "-C", str(root), *args], input=data, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


class Hosts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        seed = self.folder / "seed"
        make_repository(seed)
        self.bare = self.folder / "shared.git"
        git(self.folder, "clone", "-q", "--bare", str(seed), str(self.bare))
        self.hosts = {}

    def host(self, name, operators=("patch", "mutation", "crossover", "migration")):
        root = self.folder / name
        if not root.exists():
            git(self.folder, "clone", "-q", str(self.bare), str(root))
        doc = contract()
        doc["evolution"]["operators"] = list(operators)
        home = root / ".syberlabs"
        (home / "contracts").mkdir(parents=True, exist_ok=True)
        (home / "contracts" / "pricing-evolution.v1.json").write_text(json.dumps(doc))
        kit = Kit.local(home, root, actor=f"dev@{name}")
        self.addCleanup(kit.close)
        thread = kit.start("Fix the pricing rules", "pricing-evolution.v1")
        return kit, thread

    def provider(self, kit, host, **options):
        return EvolutionaryProvider(RuleMutator(), exchange=kit.exchange("origin", host=host, topic="pricing"),
                                    migrate_every=options.pop("migrate_every", 1), **options)

    def test_a_migrant_is_judged_by_the_host_that_receives_it(self):
        kit_a, thread_a = self.host("a")
        [fixed] = thread_a.propose(changes={"src/pricing.py": fix(PRICING, "discount", "tax", "shipping")})
        thread_a.propose(self.provider(kit_a, "a", population=2, generations=1, seeds=[fixed.id]), seed=1)
        kit_b, thread_b = self.host("b")
        found = thread_b.propose(self.provider(kit_b, "b", population=2, generations=2, crossover_every=5), seed=4)
        migrants = [c for c in found if c.operator == "migration"]
        self.assertEqual(len(migrants), 1)
        migrant = migrants[0]
        self.assertEqual(dict(migrant.origin), {"host": "a", "candidate": fixed.id})
        self.assertEqual((migrant.commit, migrant.parents), (fixed.commit, ()))
        evaluated = [e["body"]["candidate"] for e in thread_b.history() if e["kind"] == "candidate_evaluated"]
        self.assertIn(migrant.id, evaluated, "host b ran its own checks on the migrant")
        self.assertEqual((migrant.evaluation, migrant.promotion), ("passed", "provisional"))
        search = [e["body"] for e in thread_b.history() if e["kind"] == "search_finished"][-1]
        self.assertIn(migrant.id, search["recommended"])
        self.assertEqual(thread_b.accept(migrant.id).status, "succeeded")
        self.assertEqual(git(self.folder / "b", "rev-parse", thread_b.target_ref), fixed.commit)
        self.assertNotIn("syberlabs", git(self.folder / "b", "branch", "-r"))
        self.assertEqual(validate_events(thread_b.history(), "host-b"), [])

    def test_a_reported_score_is_only_a_signal(self):
        kit_a, thread_a = self.host("a")
        [broken] = thread_a.propose(changes={"src/pricing.py": PRICING.replace("return total\n", "return -1\n")})
        kit_a.exchange("origin", host="a", topic="pricing").publish(thread_a.base, [
            {"candidate": broken.id, "commit": broken.commit, "operator": "patch", "parents": [], "score": [3, 3]}])
        kit_b, thread_b = self.host("b")
        [good] = thread_b.propose(changes={"src/pricing.py": fix(PRICING, "discount")})
        provider = self.provider(kit_b, "b", population=2, generations=1, seeds=[good.id], stop_when_passing=False)
        thread_b.propose(provider, seed=2)
        migrant = next(c for c in thread_b.candidates() if c.operator == "migration")
        self.assertEqual(migrant.evaluation, "failed")
        self.assertEqual(thread_b.accept(migrant.id).reason, "candidate_check_failed:discount")
        self.assertNotIn(migrant.id, [p for t in provider.trace for p in t.get("population", [])])

    def test_untrusted_manifests_and_commits_are_refused(self):
        kit_a, thread_a = self.host("a")
        [one] = thread_a.propose(changes={"src/pricing.py": fix(PRICING, "tax")})
        [two] = thread_a.propose(changes={"src/pricing.py": fix(PRICING, "shipping")})
        exchange = kit_a.exchange("origin", host="a", topic="pricing")
        exchange.publish(thread_a.base, [{"candidate": one.id, "commit": one.commit, "operator": "patch", "parents": [], "score": [1, 1]}])
        kit_b, thread_b = self.host("b")
        receiver = kit_b.exchange("origin", host="b", topic="pricing")
        self.assertEqual([(o.host, o.candidate, o.commit) for o in receiver.offers(thread_b.base)], [("a", one.id, one.commit)])
        # Replace only the manifest so its entry names a commit the host never published under that name.
        forged = json.dumps({"protocol": "syberlabs.exchange/v0alpha1", "host": "a", "topic": "pricing", "base": thread_a.base,
                             "candidates": [{"candidate": one.id, "commit": two.commit, "score": [3, 3]}]})
        root_a = self.folder / "a"
        blob = git_input(root_a, forged, "hash-object", "-w", "--stdin")
        tree = git_input(root_a, f"100644 blob {blob}\tmanifest.json\n", "mktree")
        manifest = git(root_a, "commit-tree", tree, "-m", "forged")
        git(root_a, "push", "-q", "origin", f"+{manifest}:refs/syberlabs/exchange/pricing-{thread_a.base[:12]}/a/manifest")
        self.assertEqual(receiver.offers(thread_b.base), [], "an entry whose ref disagrees with its manifest is dropped")
        git(self.folder / "b", "checkout", "-q", "--orphan", "elsewhere")
        (self.folder / "b" / "other.txt").write_text("unrelated history\n")
        git(self.folder / "b", "add", "other.txt")
        git(self.folder / "b", "commit", "-qm", "unrelated")
        stranger = git(self.folder / "b", "rev-parse", "HEAD")
        seen = {}

        def migrate(space):
            for commit in (stranger, "f" * 40):
                try:
                    space.migrate(commit, origin={"host": "a", "candidate": "cx"})
                except Rejected as exc:
                    seen[commit] = exc.code

        thread_b.propose(FunctionProvider(migrate, name="migrator", revision="1", operators=("migration",)))
        self.assertEqual(seen, {stranger: "migration_base_mismatch", "f" * 40: "migration_unavailable"})

    def test_out_of_scope_migrants_are_recorded_not_run(self):
        kit_a, thread_a = self.host("a")
        [escape] = thread_a.propose(changes={"setup.py": "import os\nos.system('echo pwned')\n"})
        kit_a.exchange("origin", host="a", topic="pricing").publish(thread_a.base, [
            {"candidate": escape.id, "commit": escape.commit, "operator": "patch", "parents": [], "score": [3, 3]}])
        kit_b, thread_b = self.host("b")
        thread_b.propose(self.provider(kit_b, "b", population=2, generations=1), seed=1)
        migrant = next(c for c in thread_b.candidates() if c.operator == "migration")
        self.assertEqual(migrant.scope_violations, ("setup.py",))
        self.assertNotIn(migrant.id, [e["body"]["candidate"] for e in thread_b.history() if e["kind"] == "candidate_evaluated"])

    def test_the_contract_must_allow_migration(self):
        kit, thread = self.host("a", operators=("patch", "mutation", "crossover"))
        with self.assertRaises(Rejected) as caught:
            thread.propose(self.provider(kit, "a"))
        self.assertEqual(caught.exception.code, "operator_not_permitted")

    def test_hosts_write_only_their_own_namespace(self):
        kit_a, thread_a = self.host("a")
        thread_a.propose(self.provider(kit_a, "a", population=2, generations=1), seed=1)
        kit_b, thread_b = self.host("b")
        thread_b.propose(self.provider(kit_b, "b", population=2, generations=1), seed=2)
        refs = git(self.bare, "for-each-ref", "--format=%(refname)", "refs/syberlabs").splitlines()
        hosts = {ref.split("/")[4] for ref in refs}
        self.assertEqual(hosts, {"a", "b"})
        self.assertEqual(git(self.bare, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines(), ["refs/heads/main"])
        with self.assertRaises(Rejected):
            kit_a.exchange("origin", host="A/../b", topic="pricing")


if __name__ == "__main__":
    unittest.main()
