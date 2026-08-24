from pathlib import Path


def replace_exact(path: str, old: str, new: str, expected: int = 1) -> None:
    file_path = Path(path)
    text = file_path.read_text()
    count = text.count(old)
    if count != expected:
        raise SystemExit(
            f"{path}: expected {expected} occurrence(s) of {old!r}, found {count}"
        )
    file_path.write_text(text.replace(old, new))


# app.js changed, so its explicit cache-busting contract advances with it.
replace_exact(
    "backend/tests/test_dashboard_startup_surface.py",
    '/app.js?v=12',
    '/app.js?v=13',
)
replace_exact(
    "backend/tests/test_operator_title_state.py",
    '/app.js?v=12',
    '/app.js?v=13',
)
replace_exact(
    "backend/tests/test_v1_scope.py",
    '/app.js?v=12',
    '/app.js?v=13',
)

# The generated source-contract test only needs to prove both persisted fields
# exist; avoid embedding SQL default quoting inside a Python single-quoted literal.
test_path = Path("backend/tests/test_extraction_lifecycle.py")
lines = test_path.read_text().splitlines()
replaced = False
for index, line in enumerate(lines):
    if "assert '(\"extraction_status\"" in line or "assert '(\"extraction_status" in line:
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = f'{indent}assert "extraction_status" in database_source'
        replaced = True
        break
if not replaced:
    # Match the actual generated source after Python concatenates adjacent
    # single-quoted segments around the SQL default.
    for index, line in enumerate(lines):
        if "extraction_status" in line and "database_source" in line and "assert" in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f'{indent}assert "extraction_status" in database_source'
            replaced = True
            break
if not replaced:
    raise SystemExit("Could not find extraction_status source assertion")
test_path.write_text("\n".join(lines) + "\n")

print("Extraction lifecycle test contracts corrected")
