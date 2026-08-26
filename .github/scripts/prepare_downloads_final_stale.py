from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_downloads_final_stale.py <helper>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
marker = "# Add explicit contracts for this reviewed correction batch.\n"
if text.count(marker) != 1:
    raise SystemExit("could not locate correction-contract marker")

insertion = '''# Two older contracts use literal forms that are intentionally outside the\n# generic slash-prefixed cache mapping above. Keep these migrations scoped.\nreplace_exact(\n    TESTS / "test_ui_desktop_downloads_batch_contract.py",\n    'style.index("ui-transfer-contract.css?v=30")',\n    'style.index("ui-transfer-contract.css?v=31")',\n)\n_deep_audit = TESTS / "test_ui_frontend_deep_audit_contract.py"\n_deep_text = _deep_audit.read_text(encoding="utf-8")\nif _deep_text.count("v=22$") != 1:\n    raise SystemExit("deep-audit fallback generation guard did not match exactly once")\n_deep_audit.write_text(_deep_text.replace("v=22$", "v=23$"), encoding="utf-8")\n\n'''

path.write_text(text.replace(marker, insertion + marker), encoding="utf-8")
