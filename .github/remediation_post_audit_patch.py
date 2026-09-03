from pathlib import Path

path = Path('.github/remediation_post_audit.py')
text = path.read_text(encoding='utf-8')
old = 'This document describes the post-audit canonical frontend ownership model. It records final owners, not the historical correction layers used while the UI overhaul was being built.\n\n## Core rule'
new = 'This document describes the post-audit canonical frontend ownership model. It records final owners, not the historical correction layers used while the UI overhaul was being built. The frontend architecture reports `1.0.12` for the current development tree and does not promote that tree as a final release baseline.\n\n## Core rule'
if text.count(old) != 1:
    raise SystemExit(f'UI architecture wording anchor mismatch: {text.count(old)}')
path.write_text(text.replace(old, new, 1), encoding='utf-8')
