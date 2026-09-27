"""Schema checks for published examples and for events the golden run emits."""

import unittest

from spec.validate import validate_examples, validate_golden


class ProtocolExamples(unittest.TestCase):
    def test_examples_match_v0alpha1(self):
        self.assertEqual(validate_examples(), [])

    def test_golden_run_events_match_v0alpha1(self):
        self.assertEqual(validate_golden(), [])


if __name__ == "__main__":
    unittest.main()
