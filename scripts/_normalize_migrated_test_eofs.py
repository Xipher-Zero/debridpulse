from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "backend" / "tests"

# The AST contract migration can remove the terminal test in a module, leaving
# multiple trailing newlines. Normalize Python test files to exactly one EOF
# newline so git diff --check remains an integrity gate rather than a formatter.
changed = 0
for path in sorted(TESTS.glob("test_*.py")):
    text = path.read_text(encoding="utf-8")
    normalized = text.rstrip("\n") + "\n"
    if normalized != text:
        path.write_text(normalized, encoding="utf-8")
        changed += 1

print(f"Normalized EOF newline in {changed} migrated test files")
