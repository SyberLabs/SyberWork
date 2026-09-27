import sys
import unittest


class SdkPackage(unittest.TestCase):
    def test_import_uses_stdlib_only(self):
        sys.modules.pop("syberlabs", None)
        sys.modules.pop("syberwork", None)
        sys.modules.pop("syberwork.core", None)
        import syberlabs
        self.assertIn("canonical", syberlabs.__all__)
        self.assertNotIn("syberwork", sys.modules)
        self.assertNotIn("syberwork.core", sys.modules)
