from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "backend" / "tests" / "test_v1111_canonical_frontend_contract.py"
text = path.read_text(encoding="utf-8")

replacements = {
    "dp-settings-authentication-status-card": "dp-settings-auth-status-card",
    "dp-auth-oidc-callback-url": "dp-auth-oidc-callback",
}
for old, new in replacements.items():
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one canonical contract selector {old!r}, found {count}")
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("Corrected canonical v1.0.11.1 contract selectors")
