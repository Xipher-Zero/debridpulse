"""Native defaults of the decorative material and chrome tokens.

The reviewed DebridPulse Dark and Light values that page owners once stated as
literals are the defaults of the tokens those owners now consume
(frontend/static/ui-language-tokens.css). A contract that protects such a
value asserts both halves: the owner uses the token, and the token's native
default is still the reviewed value.
"""

import re
from pathlib import Path

TOKENS = Path(__file__).resolve().parents[2] / "frontend" / "static" / "ui-language-tokens.css"


def token_defaults(name: str) -> str:
    """Every native declaration of `name` (dark and light), joined."""
    source = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(encoding="utf-8"), flags=re.S)
    values = re.findall(rf"(?<![\w-]){re.escape(name)}\s*:\s*([^;]+);", source)
    assert values, f"{name} has no native default in ui-language-tokens.css"
    return "\n".join(" ".join(value.split()) for value in values)


def require_token(owner_css: str, name: str, *reviewed: str) -> None:
    assert f"var({name})" in owner_css, f"the owner no longer consumes {name}"
    defaults = token_defaults(name)
    missing = [fragment for fragment in reviewed if fragment not in defaults]
    assert not missing, f"{name} lost its reviewed native value(s): {missing}"
