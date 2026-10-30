"""Synthetic engineering recipe: no formal pack, real credentials or paid calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import githits_engineering_smoke as smoke
from tests.test_pilot_dry_run import DryRunCase, LOOKUPS, answers, in_process


SOLUTIONS = {
    "src/smoke_csv.py": 'import csv, io\ndef render(rows):\n    out = io.StringIO(newline="")\n    w = csv.DictWriter(out, fieldnames=["name", "qty"])\n    w.writeheader()\n    w.writerows(rows)\n    return out.getvalue()\n',
    "src/smoke_counts.py": 'from collections import Counter\ndef summarize(values):\n    return dict(sorted(Counter(values).items()))\n',
    "src/smoke_utf8.py": 'import codecs\ndef decode_chunks(chunks):\n    decoder = codecs.getincrementaldecoder("utf-8")()\n    return "".join(decoder.decode(c) for c in chunks) + decoder.decode(b"", final=True)\n',
}


class Preparation(unittest.TestCase):
    def test_frozen_base_repeats_and_checks_reject_the_base_and_wrong_code(self):
        with tempfile.TemporaryDirectory() as folder:
            first, plan = smoke.prepare(Path(folder) / "one")
            second, _ = smoke.prepare(Path(folder) / "two")
            self.assertEqual(smoke.pilot.git(first, "rev-parse", "HEAD"), smoke.pilot.git(second, "rev-parse", "HEAD"))
            resolved = smoke.pilot.load_plan(plan)
            self.assertEqual(len(resolved["items"]), 3)
            self.assertEqual(resolved["allowed_tools"], "Read,Edit,Write,Glob,Grep")
            self.assertEqual(list(resolved["prices"]), [smoke.MODEL])
            command = resolved["checks"]["engineering-smoke"]["argv"]
            self.assertNotEqual(subprocess.run(command, cwd=first, capture_output=True).returncode, 0)
            for name, code in SOLUTIONS.items():
                path = first / name
                path.write_text("def render(x): return ''\ndef summarize(x): return {}\ndef decode_chunks(x): return ''\n")
                self.assertNotEqual(subprocess.run(command, cwd=first, capture_output=True).returncode, 0, name)
                path.write_text(code, encoding="utf-8")
                done = subprocess.run(command, cwd=first, capture_output=True)
                self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
                path.unlink()

    def test_preparation_never_calls_a_provider_and_a_wrong_pin_is_refused(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(smoke.pilot, "main") as run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(smoke.main(["--work", str(Path(folder) / "bad"), "--factory-commit", "0" * 40]), 2)
                pin = smoke.pilot.host_checkout()["commit"]
                self.assertEqual(smoke.main(["--work", str(Path(folder) / "ready"), "--factory-commit", pin]), 0)
            run.assert_not_called()
            self.assertFalse((Path(folder) / "bad").exists())

    def test_the_cli_pin_is_exactly_2_1_312_recorded_and_any_other_version_is_refused(self):
        """pilot-readiness, item 3: the host and the pilot VM run Claude Code 2.1.312, so the frozen recipe pins it,
        exactly, and records the pin in the plan and the manifest."""
        self.assertEqual(smoke.CLI_VERSION, "2.1.312")
        plan = json.loads((smoke.PACK / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["_configuration"]["cli"], smoke.CLI_VERSION)
        with tempfile.TemporaryDirectory() as folder:
            smoke.prepare(Path(folder) / "ready")
            manifest = json.loads((Path(folder) / "ready" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["cli_version"], smoke.CLI_VERSION)
            pin = smoke.pilot.host_checkout()["commit"]
            script = Path(folder) / "claude_stand_in.py"
            for printed, refused in (("2.1.311 (Claude Code)", True), ("2.1.3120 (Claude Code)", True),
                                     ("2.1.312 (Claude Code) beta", True), ("banner\n2.1.312 (Claude Code)", True),
                                     ("2.1.312 (Claude Code)", False)):
                with self.subTest(version=printed):
                    script.write_text(f"print({printed!r})\n", encoding="utf-8")
                    work = Path(folder) / f"run-{len(list(Path(folder).iterdir()))}"
                    with mock.patch.dict(os.environ, {smoke.pilot.claude_code.BIN_ENV: f"{sys.executable} {script}"}), \
                            mock.patch.object(smoke.pilot, "host_checkout", return_value={"commit": pin, "dirty": False}), \
                            mock.patch.object(smoke.pilot, "main", return_value=0) as run, \
                            contextlib.redirect_stdout(io.StringIO()) as out:
                        code = smoke.main(["--work", str(work), "--factory-commit", pin, "--run-approved"])
                    if refused:
                        self.assertEqual(code, 2, out.getvalue())
                        self.assertIn("requires Claude Code 2.1.312 exactly", out.getvalue())
                        run.assert_not_called()
                    else:
                        self.assertEqual(code, 0, out.getvalue())
                        self.assertIn("Claude Code 2.1.312 (the exact pin)", out.getvalue())
                        run.assert_called_once()


class OfflineRecipe(DryRunCase):
    def test_one_recipe_runs_the_real_three_item_fixture_with_stand_ins(self):
        command = self.stand_in("engineering", version=smoke.CLI_VERSION + " (Claude Code)", lookups=LOOKUPS)
        path = Path(command[-1])
        text = path.read_text(encoding="utf-8")
        # Select visible synthetic task implementations; the canary changes no file.
        choose = "\nsolutions = " + repr(SOLUTIONS) + "\nconfig['first'] = next(({n: s} for n, s in solutions.items() if n in prompt), {})\n"
        text = text.replace('repair = "did not pass the host\'s checks" in prompt',
                            choose + 'repair = "did not pass the host\'s checks" in prompt')
        text = text.replace('"claude-opus-5"', '"claude-sonnet-5-5"')
        path.write_text(text, encoding="utf-8")
        os.environ[smoke.pilot.claude_code.BIN_ENV] = " ".join(command)
        actual_main = smoke.pilot.main
        pin = smoke.pilot.host_checkout()["commit"]
        work = self.root.parent / "engineering-recipe"
        def run(argv):
            return actual_main(argv, runner=in_process, ask=answers("y", *["pass"] * 6))
        with mock.patch.object(smoke.pilot, "main", side_effect=run), \
                mock.patch.object(smoke.pilot, "host_checkout", return_value={"commit": pin, "dirty": False}), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = smoke.main(["--work", str(work), "--factory-commit", pin, "--run-approved"])
        receipt = json.loads((work / "receipts" / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual((code, receipt["verdict"]), (0, "PASS"), printed.getvalue()[-3000:])
        self.assertEqual(receipt["report"]["compared"], 3)
        self.assertEqual(receipt["canary"]["status"], "PASS")
        self.assertEqual(len(self.fake.requests), 5)
        self.assertEqual(receipt["credential_scan"]["found"], [])
        for secret in (self.token, self.api_key):
            self.assertNotIn(secret, printed.getvalue())


if __name__ == "__main__":
    unittest.main()
