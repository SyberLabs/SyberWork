from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples import attest_dispatch

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "examples" / "attest_dispatch.py"


def sample_ticket() -> dict[str, object]:
    return {
        "schema": "attest.ticket/v0",
        "case_id": "case-90",
        "title": "Missing capability: Won",
        "spec": {"source": "demo/spec.md", "lines": [15, 15], "text": "Mark as won"},
        "acceptance": "The deal status changes to Won.",
        "observed": {"status": "failed", "observed": {"c05_label": ""}, "note": "No button"},
        "app": {"root": "demo/app", "files": {"app.js": "const status = 'Open';\n"}},
        "output": "unified diff in git format with paths a/<file> and b/<file> relative to app.root",
    }


def write_worker_config(directory: Path, command: str) -> Path:
    config = directory / "workers.json"
    config.write_text(json.dumps({"default": {"type": "command", "cmd": command}}), encoding="utf-8")
    return config


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class AttestDispatchExampleTests(unittest.TestCase):
    def test_command_worker_prints_only_the_exact_diff(self) -> None:
        expected_diff = (
            "diff --git a/app.js b/app.js\n"
            "--- a/app.js\n"
            "+++ b/app.js\n"
            "@@ -1 +1 @@\n"
            "-const status = 'Open';\n"
            "+const status = 'Won';\n"
        )
        crlf_diff = expected_diff.replace("\n", "\r\n")
        fake_command = (
            "import json,sys; ticket=json.load(sys.stdin); "
            "assert ticket['schema']=='attest.ticket/v0'; "
            f"sys.stdout.buffer.write({crlf_diff!r}.encode('utf-8'))"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_worker_config(Path(temp_dir), shlex.join([sys.executable, "-c", fake_command]))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--workers", str(config)],
                input=json.dumps(sample_ticket()),
                text=True,
                capture_output=True,
                cwd=REPOSITORY,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, expected_diff)
        self.assertNotIn("attest_ticket", completed.stdout)
        self.assertIn('"verified": false', completed.stderr)
        self.assertIn("admitted case case-90", completed.stderr)

    def test_example_config_defaults_to_openai_and_selects_command(self) -> None:
        default = attest_dispatch.load_worker_config(attest_dispatch.DEFAULT_WORKERS)
        self.assertEqual(default, {"type": "openai", "model": "gpt-5-mini"})

        command = attest_dispatch.load_worker_config(attest_dispatch.DEFAULT_WORKERS, "command")
        self.assertEqual(command["type"], "command")
        self.assertEqual(command["argv"][0], sys.executable)
        self.assertTrue(Path(command["argv"][1]).is_file())

    def test_default_openai_worker_requires_a_key(self) -> None:
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("SYBERWORK_OPENAI_API_KEY", None)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(sample_ticket()),
            text=True,
            capture_output=True,
            cwd=REPOSITORY,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("OPENAI_API_KEY is not set", completed.stderr)

    def test_command_worker_is_an_explicit_offline_opt_in(self) -> None:
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("SYBERWORK_OPENAI_API_KEY", None)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--worker", "command"],
            input=json.dumps(sample_ticket()),
            text=True,
            capture_output=True,
            cwd=REPOSITORY,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.startswith("diff --git a/app.js b/app.js\n"))
        self.assertNotIn("attest_ticket", completed.stdout)
        attest_dispatch.validate_git_diff(completed.stdout)
        self.assertIn('"verified": false', completed.stderr)

    def test_malformed_ticket_fails_with_a_reason_on_stderr(self) -> None:
        ticket = sample_ticket()
        ticket["schema"] = "wrong/v0"
        with tempfile.TemporaryDirectory() as temp_dir:
            config = write_worker_config(Path(temp_dir), shlex.join([sys.executable, "-c", "pass"]))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--workers", str(config)],
                input=json.dumps(ticket),
                text=True,
                capture_output=True,
                cwd=REPOSITORY,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertIn("ticket schema must be attest.ticket/v0", completed.stderr)

    def test_worker_output_with_prose_is_rejected(self) -> None:
        patch_text = (
            "diff --git a/app.js b/app.js\n"
            "--- a/app.js\n+++ b/app.js\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "Here is the diff.\n"
        )
        with self.assertRaisesRegex(attest_dispatch.DispatchError, "text outside the Git-format diff"):
            attest_dispatch.validate_git_diff(patch_text)

    def test_empty_app_files_and_missing_root_are_rejected(self) -> None:
        ticket = sample_ticket()
        ticket["app"] = {"root": "demo/app", "files": {}}
        with self.assertRaisesRegex(attest_dispatch.DispatchError, "app.files must be a nonempty object"):
            attest_dispatch.validate_ticket(ticket)
        ticket["app"] = {"files": {"app.js": "content"}}
        with self.assertRaisesRegex(attest_dispatch.DispatchError, "app.root must be a nonempty string"):
            attest_dispatch.validate_ticket(ticket)

    def test_openai_key_supports_the_alternate_secret_environment_name(self) -> None:
        with patch.dict("os.environ", {"SYBERWORK_OPENAI_API_KEY": "test-only-key"}, clear=True):
            self.assertEqual(attest_dispatch.openai_api_key(), "test-only-key")

    def test_openai_uses_structured_contents_and_builds_the_git_diff_locally(self) -> None:
        ticket = attest_dispatch.validate_ticket(sample_ticket())
        structured = json.dumps({"files": [{"path": "app.js", "content": "const status = 'Won';\n"}]})
        fake_response = FakeResponse({"output_text": structured})
        with patch.object(attest_dispatch.urllib.request, "urlopen", return_value=fake_response) as urlopen:
            patch_text = attest_dispatch.openai_worker(ticket, "gpt-5-mini", "not-a-real-key")

        self.assertTrue(patch_text.startswith("diff --git a/app.js b/app.js\n"))
        self.assertIn("--- a/app.js\n+++ b/app.js\n", patch_text)
        request = urlopen.call_args.args[0]
        request_body = json.loads(request.data)
        self.assertEqual(request_body["model"], "gpt-5-mini")
        self.assertEqual(request_body["text"]["format"]["type"], "json_schema")
        instructions = request_body["input"][0]["content"]
        self.assertIn("complete contents", instructions)
        self.assertIn("structured output schema", instructions)
        self.assertNotIn("unified diff", instructions.lower())
        self.assertNotIn("--- a/", instructions)
        self.assertNotIn("+++ b/", instructions)
        self.assertNotIn("Begin Patch", instructions)


if __name__ == "__main__":
    unittest.main()
