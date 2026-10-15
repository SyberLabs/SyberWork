"""Upstream provenance, blinding boundaries and frozen-definition enforcement."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.githits import upstream_v3 as suite
from benchmarks.githits.upstream_fixtures import UPSTREAM_FIXTURES


class UpstreamRegistration(unittest.TestCase):
    def test_original_bytes_match_upstream_blob_and_digest(self):
        self.assertEqual(list(UPSTREAM_FIXTURES), ["U01", "U02", "U03", "U04"])
        for fixture in UPSTREAM_FIXTURES.values():
            data = fixture["broken"].encode()
            self.assertEqual(hashlib.sha256(data).hexdigest(), fixture["provenance"]["broken_sha256"])
            blob = b"blob " + str(len(data)).encode() + b"\0" + data
            self.assertEqual(hashlib.sha1(blob).hexdigest(), fixture["provenance"]["upstream_blob"])
            self.assertNotEqual(fixture["broken"], fixture["reference"])
            self.assertTrue(fixture["blind"])
            self.assertNotIn("with_githits", fixture["checks"])
            self.assertNotIn("without_githits", fixture["checks"])
            self.assertIn("importlib.import_module(MODULE)", fixture["checks"])

    def test_definition_and_source_mutations_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registration.json"
            suite.freeze(path)
            original = json.loads(path.read_text())
            with patch.object(suite, "REGISTRATION", path), patch.object(suite, "ROOT", Path(tmp)), \
                    patch.object(suite, "git", return_value=json.dumps(original)), \
                    patch.object(suite, "source_hashes", return_value=original["source_sha256_lf"]):
                suite.registration()
                for key in ("configuration", "fixture_sha256", "source_sha256_lf"):
                    changed = copy.deepcopy(original)
                    changed[key] = {}
                    path.write_text(json.dumps(changed))
                    with self.subTest(key=key), self.assertRaisesRegex(ValueError, "upstream_registration_changed"):
                        suite.registration()
                path.write_text(json.dumps(original))
                with patch.object(suite, "source_hashes", return_value={}):
                    with self.assertRaises(ValueError): suite.registration()
            with self.assertRaisesRegex(ValueError, "refuse_to_overwrite"):
                suite.freeze(path)

    def test_context_must_match_exact_upstream_snapshot(self):
        fixture = UPSTREAM_FIXTURES["U01"]
        excerpt = {"path": fixture["reads"][0]["path"], "content": "test-only-content",
                   "target_resolution": {"served": {"commit_sha": fixture["provenance"]["tag_commit"],
                                                       "repo_url": fixture["provenance"]["repository"]}}}
        expected = copy.deepcopy(fixture)
        expected["expected_context_sha256"][excerpt["path"]] = suite.digest(excerpt["content"].encode())
        suite.check_upstream_excerpt(expected, excerpt)
        for key in ("commit_sha", "repo_url"):
            changed = copy.deepcopy(excerpt)
            changed["target_resolution"]["served"][key] = "incorrect"
            with self.subTest(key=key), self.assertRaises(ValueError):
                suite.check_upstream_excerpt(expected, changed)
        changed = {**excerpt, "content": "changed"}
        with self.assertRaises(ValueError): suite.check_upstream_excerpt(expected, changed)


if __name__ == "__main__":
    unittest.main()
