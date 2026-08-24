from pathlib import Path

for name in (
    "backend/tests/test_extraction_pulse.py",
    "backend/tests/test_v1_scope.py",
):
    path = Path(name)
    text = path.read_text()
    count = text.count('/style.css?v=14')
    if count != 1:
        raise SystemExit(f"{name}: expected one v14 style assertion, found {count}")
    path.write_text(text.replace('/style.css?v=14', '/style.css?v=15', 1))

print("Updated stylesheet cache-bust assertions")
