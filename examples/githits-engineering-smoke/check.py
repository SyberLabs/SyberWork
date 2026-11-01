"""Visible, synthetic host checks. No formal pilot data or hidden answers."""
import csv
import importlib.util
import io
import json
from pathlib import Path


def check(root):
    modules = [root / "src" / name for name in ("smoke_csv.py", "smoke_counts.py", "smoke_utf8.py")]
    present = [path for path in modules if path.is_file()]
    assert len(present) == 1, "one task module must exist; the base intentionally fails"
    path = present[0]
    spec = importlib.util.spec_from_file_location("smoke_subject", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if path.name == "smoke_csv.py":
        for rows in ([], [{"name": 'a,"b', "qty": 2}, {"name": "pi-\u03c0\nline", "qty": -1}]):
            result = module.render(rows)
            assert isinstance(result, str), "CSV must be text"
            parsed = list(csv.reader(io.StringIO(result, newline="")))
            assert parsed == [["name", "qty"], *[[row["name"], str(row["qty"])] for row in rows]]
            assert result.endswith("\r\n"), "CSV requires CRLF"
    elif path.name == "smoke_counts.py":
        for values, expected in (([], {}), (["b", "A", "b", "\u03c0", "A"], {"A": 2, "b": 2, "\u03c0": 1})):
            result = module.summarize(values)
            assert result == expected and list(result) == sorted(expected), "counts and sorted key order"
            json.dumps(result, allow_nan=False)
    else:
        for text in ("", "ASCII", "pi-\u03c0-\U0001f680"):
            data = text.encode("utf-8")
            assert module.decode_chunks([data[n:n + 1] for n in range(len(data))]) == text
        for chunks in ([b"\xff"], [b"\xe2\x82"]):
            try:
                module.decode_chunks(chunks)
            except UnicodeDecodeError:
                pass
            else:
                raise AssertionError("invalid or incomplete UTF-8 must fail strictly")
    print("ENGINEERING SMOKE checks passed:", path.name)


if __name__ == "__main__":
    check(Path.cwd())
