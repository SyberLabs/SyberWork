"""The new suite cannot silently bypass or change its preregistration."""

import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmarks.githits import codex_adapter, preregister, run
from benchmarks.githits.fixtures import LEGACY_FIXTURES, HARD_FIXTURES


class Registration(unittest.TestCase):
    def test_original_defaults_remain_three_separate_tasks(self):
        self.assertEqual(len(LEGACY_FIXTURES), 3)
        self.assertEqual(list(HARD_FIXTURES), [f"T{i:02}" for i in range(1, 7)])
        self.assertFalse(set(LEGACY_FIXTURES) & set(HARD_FIXTURES))

    def test_frozen_configuration_and_fixtures_cannot_change(self):
        original = preregister.build_registration()
        preregister.check_registration(original)
        for field in ("configuration", "tasks", "fixture_sha256", "source_sha256_lf"):
            changed = copy.deepcopy(original)
            if field == "tasks":
                changed[field] = changed[field][:-1]
            else:
                changed[field].clear()
            with self.subTest(field=field), self.assertRaises(ValueError):
                preregister.check_registration(changed)

    def test_changed_source_bytes_are_rejected(self):
        registration = preregister.build_registration()
        path = next(iter(registration["source_sha256_lf"]))
        registration["source_sha256_lf"][path] = "0" * 64
        with self.assertRaisesRegex(ValueError, "preregistered_source_changed"):
            preregister.check_registration(registration)

    def test_hard_tasks_cannot_use_unregistered_original_route(self):
        with patch.object(sys, "argv", ["run", "--python", sys.executable, "--write", "unused", "--fixtures", "T01"]), \
                patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as caught:
            run.main()
        self.assertEqual(caught.exception.code, 2)

    def test_blinded_prompt_has_no_arm_brand_checks_or_reference(self):
        context = [{"target":"pypi:httpx@0.28.1", "content":"public dependency excerpt"}]
        captured = []
        def cli(argv, **kwargs):
            if "--version" in argv:
                return SimpleNamespace(stdout="test-cli")
            captured.append(kwargs["input"])
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps({"message":"m","rationale":"r","files":[]}))
            return SimpleNamespace(returncode=0, stdout=b'{"type":"turn.completed","usage":{}}', stderr=b"")
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(codex_adapter, "build_prompt", return_value="objective and current source only"), \
                patch.object(codex_adapter.shutil, "which", return_value="codex"), \
                patch.object(codex_adapter.subprocess, "run", side_effect=cli):
            codex_adapter.respond({}, Path(folder)/"call", context, "model", "high", blind=True)
        prompt = captured[0].decode()
        self.assertIn("public dependency excerpt", prompt)
        for forbidden in ("GitHits", "with_githits", "without_githits", "class Behavior", "reference fix"):
            self.assertNotIn(forbidden, prompt)
