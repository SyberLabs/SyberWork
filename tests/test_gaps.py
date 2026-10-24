"""``spec/GAPS.md``: one number per entry, in file order; references that name an entry; refusal reasons in prose.

pre-pilot-cleanup (2026-10-23). On 029d27c7e1 the stacked branches had left the entries numbered out of order
(39, 37, 40, 38, 41, 39) and two entries each under 46, 49, 50 and 51; ``Numbering`` fails on that file. The prose
lists of ``search.REFUSAL_REASONS`` named seven reasons after ``instruction_files_changed`` became the eighth;
``RefusalReasons`` fails on those lists.
"""

import re
import unittest
from pathlib import Path

from syberlabs.search import REFUSAL_REASONS

ROOT = Path(__file__).resolve().parents[1]
GAPS = ROOT / "spec" / "GAPS.md"
ENTRY = re.compile(r"^(\d+)\. ", re.M)
# The forms references take: ``spec/GAPS.md``, 32 / `spec/GAPS.md` entry 45 / `spec/GAPS.md` 54, 55 and 56 /
# GAPS entries 26 and 28 / (GAPS 30 and 31).
REFERENCE = re.compile(r"GAPS(?:\.md)?`{0,2},? (?:entr(?:y|ies) )?(\d+(?:(?:, | and | to )\d+)*)")
# Where each prose list of the refusal reasons lives: (file, a phrase of the paragraph that holds the list).
REFUSAL_LISTS = (("docs/BUILD_THREAD.md", "a reason from a closed set"),
                 ("docs/EVOLUTION.md", "`reason` is one of a closed set"),
                 ("spec/GAPS.md", "with `reason` from the closed set"))


def entry_numbers(text: str) -> list[int]:
    return [int(n) for n in ENTRY.findall(text)]


def numbering_problems(numbers: list[int]) -> list[str]:
    """What is wrong with a list of entry numbers: a number used twice, or one not one more than the entry before."""
    found = [f"{n} is shared" for n in sorted({n for n in numbers if numbers.count(n) > 1})]
    found += [f"entry {i} is numbered {n}" for i, n in enumerate(numbers, 1) if n != i]
    return found


class Numbering(unittest.TestCase):
    def test_every_entry_has_one_number_in_file_order(self):
        numbers = entry_numbers(GAPS.read_text(encoding="utf-8"))
        self.assertGreater(len(numbers), 50)
        self.assertEqual(numbering_problems(numbers), [])

    def test_a_shared_or_decreasing_number_is_a_problem(self):
        self.assertEqual(numbering_problems([1, 2, 3]), [])
        self.assertIn("2 is shared", numbering_problems([1, 2, 2, 3]))
        self.assertTrue(numbering_problems([1, 3, 2]), "out of order")
        self.assertTrue(numbering_problems([2, 3]), "not from 1")
        self.assertEqual(entry_numbers("1. **a**\n\n    *closed.* 7. not an entry\n2. **b**\n"), [1, 2])

    def test_every_reference_names_an_entry_that_exists(self):
        count = len(entry_numbers(GAPS.read_text(encoding="utf-8")))
        files = [ROOT / "AGENTS.md", ROOT / "README.md", *sorted(ROOT.glob("docs/*.md")),
                 *sorted(ROOT.glob("syberlabs/*.py")), *sorted(ROOT.glob("tests/*.py"))]
        seen = 0
        for path in files:
            if not path.is_file():
                continue
            for match in REFERENCE.finditer(path.read_text(encoding="utf-8")):
                for number in re.findall(r"\d+", match.group(1)):
                    seen += 1
                    self.assertTrue(1 <= int(number) <= count, f"{path.name}: {match.group(0)!r}")
        self.assertGreater(seen, 20, "the references were found")


class RefusalReasons(unittest.TestCase):
    def test_every_prose_list_names_every_reason(self):
        for name, phrase in REFUSAL_LISTS:
            lines = [line for line in (ROOT / name).read_text(encoding="utf-8").splitlines() if phrase in line]
            self.assertEqual(len(lines), 1, f"{name}: one paragraph holds the list")
            missing = [reason for reason in REFUSAL_REASONS if f"`{reason}`" not in lines[0]]
            self.assertEqual(missing, [], f"{name} does not name every reason in search.REFUSAL_REASONS")


if __name__ == "__main__":
    unittest.main()
