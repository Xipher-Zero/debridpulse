"""The interface's localization runtime for tests that run frontend code under Node.

A function under test says what the page says: its words come from the real
runtime (``frontend/static/ui-i18n.js``), formatted by the vendored FormatJS
formatter, from the real English catalog -- no stub messages and no second
formatter. ``program(body)`` returns one Node program (for ``node -e``): the
runtime made ready, then ``body``, run inside an async function (so it may
``await``). The program names the files rather than holding them, so it stays
within the length of one command-line argument.

A harness that assigns its own ``window`` object keeps the runtime: the
assignment hands it ``DPI18n``, as the page's own window has it.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def _formatter_module() -> Path:
    """The vendored formatter as an ``.mjs`` file, so any Node loads it as a module."""
    source = (STATIC / "vendor" / "intl-messageformat.esm.js").read_bytes()
    target = Path(tempfile.gettempdir()) / f"dp-intl-messageformat-{hashlib.sha256(source).hexdigest()[:16]}.mjs"
    if not target.is_file():
        partial = target.with_suffix(f".{hashlib.sha256(str(id(source)).encode()).hexdigest()[:8]}.part")
        partial.write_bytes(source)
        partial.replace(target)
    return target


def program(body: str) -> str:
    runtime = json.dumps(str(STATIC / "ui-i18n.js"))
    catalog = json.dumps(str(STATIC / "i18n" / "en.json"))
    formatter = json.dumps(_formatter_module().as_uri())
    return f"""
(async () => {{
  const fs = require('fs');
  const pageWindow = globalThis;
  if (typeof globalThis.window === 'undefined') globalThis.window = globalThis;
  const noop = () => {{}};
  const root = {{lang: 'en', dir: 'ltr', setAttribute: noop, removeAttribute: noop, getAttribute: () => null}};
  if (typeof globalThis.document === 'undefined') {{
    globalThis.document = {{documentElement: root, querySelectorAll: () => [], dispatchEvent: noop,
      addEventListener: noop, removeEventListener: noop, createElement: () => ({{}}), head: {{appendChild: noop}}}};
  }}
  require('vm').runInThisContext(fs.readFileSync({runtime}, 'utf8'), {{filename: 'ui-i18n.js'}});
  const {{IntlMessageFormat}} = await import({formatter});
  window.DPI18n.attachFormatter(IntlMessageFormat);
  window.DPI18nCatalog('en', JSON.parse(fs.readFileSync({catalog}, 'utf8')).messages);
  const runtime = pageWindow.DPI18n;
  globalThis.DPI18n = runtime;
  let shown = pageWindow;
  Object.defineProperty(globalThis, 'window', {{
    configurable: true,
    get: () => shown,
    set: value => {{ shown = value; if (value && typeof value === 'object' && !value.DPI18n) value.DPI18n = runtime; }},
  }});
{body}
}})().catch(error => {{ console.error(error && error.stack || String(error)); process.exit(1); }});
"""


def english(key: str) -> str:
    """A key's English message in the catalog (what the page says in English)."""
    return json.loads((STATIC / "i18n" / "en.json").read_text(encoding="utf-8"))["messages"][key]
