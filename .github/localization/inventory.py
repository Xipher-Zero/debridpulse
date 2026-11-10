"""DebridPulse localization census (WS0).

Reproducibly lists every human-visible, first-party authored string the
interface can show, from source, so coverage is reconciled against the real
renderers rather than a hand-kept list:

* ``frontend/static/*.js``: every string and template literal, read by
  ``jslex``. Markup-bearing literals are split into messages at block
  boundaries (paragraphs, list items, headings, cells, buttons…), keeping
  inline markup (``<b>``, ``<code>``, links…) inside the message as rich text,
  and their human attributes (``aria-label``, ``title``, ``placeholder``,
  ``data-tooltip``, ``alt``) as their own entries. A literal whose surrounding
  code shows it is machine data (a selector, a class, an event or route name,
  a ``console`` diagnostic, a comparison value) is not a message.
* ``frontend/static/*.html``: text and the same human attributes.
* ``backend`` (not tests): messages the server returns to the browser for
  display -- ``HTTPException(detail=…)`` (keyword or positional), the refusal
  exceptions whose text becomes a ``detail``, and the ``"message"`` /
  ``"detail"`` of a structured refusal under ``api/`` and ``main.py`` --
  listed separately, because they are a contract (``detail`` stays English
  for API clients; the interface localizes by code). Each records the
  exception type it is raised as and whether its raise names a code
  (``code=…``, or a structured detail's ``"code"``); one that names none is
  reconciled by a reasoned exemption (``nonlocalizable.json``, by owner and
  English, as for the interface).

Every entry has an inventory ID (stable while its owner, context and English
text are unchanged), the English text, owner and line, a category, the
placeholders it carries, and a criticality. Conditionally rendered variants
are all present because the census reads source, not one rendered context.

A literal that is a key of the English catalog is a key reference (the
interface says the catalog's words), and markup that names a key
(``data-i18n``, ``data-i18n-attr``) is keyed: neither is a literal. A string
that must not be translated (a product name, a class list, a developer
diagnostic that is never shown) is listed once, with the reason, in
``nonlocalizable.json`` beside this file; the census reports those as
``exempt``, apart from the entries still to be keyed.

Usage: ``python .github/localization/inventory.py [--json OUT] [--summary]``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jslex import literals  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BACKEND = ROOT / "backend"
EXEMPTIONS = Path(__file__).resolve().parent / "nonlocalizable.json"
_catalog = STATIC / "i18n" / "en.json"
CATALOG_KEYS = frozenset(json.loads(_catalog.read_text(encoding="utf-8"))["messages"]) if _catalog.is_file() else frozenset()

HUMAN_ATTRIBUTES = ("aria-label", "title", "placeholder", "data-tooltip", "alt", "aria-description",
                    "data-menu-label", "data-default-label", "data-short-label", "data-title", "data-subtitle",
                    "data-label-1", "data-label-2", "data-limit-message", "data-fit-2", "data-fit-3",
                    "data-short", "data-shortest", "data-placeholder-concise", "data-placeholder-minimum")
TEXT_ARIA = ("aria-label", "aria-description", "aria-roledescription", "aria-valuetext", "aria-placeholder")
BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "summary", "td", "th", "button", "label", "option",
              "div", "section", "article", "header", "footer", "dt", "dd", "caption", "legend", "figcaption",
              "small", "title", "nav", "ul", "ol", "table", "tr", "details", "select", "textarea", "form"}
INLINE_TAGS = {"b", "strong", "i", "em", "code", "kbd", "a", "span", "br", "sup", "sub", "u", "mark", "abbr", "q"}

# Code that shows the literal right after it is machine data, not a message.
MACHINE_CONTEXT = re.compile(
    r"(console\.\w+\(|querySelector(All)?\(|getElementById\(|closest\(|matches\(|classList\.\w+\(|"
    r"addEventListener\(|removeEventListener\(|dispatchEvent\(|new CustomEvent\(|CustomEvent\(|"
    r"getAttribute\(|removeAttribute\(|hasAttribute\(|toggleAttribute\(|getPropertyValue\(|setProperty\(|"
    r"removeProperty\(|createElement\(|createElementNS\(|request\(\s*$|fetch\(|localStorage\.\w+\(|"
    r"sessionStorage\.\w+\(|matchMedia\(|import\(|require\(|DPIcons\??\.svg\(|familyIcon\(|protocolIcon\(|"
    r"===\s*$|!==\s*$|==\s*$|!=\s*$|case\s*$|\.includes\(|\.startsWith\(|\.endsWith\(|\.indexOf\(|split\(|"
    r"\.replace(All)?\(|\.join\(|new RegExp\(|\.test\(|\.match\(|typeof\s+\w+\s*$|dataset\.\w+\s*=\s*$|"
    r"\.dataset\[\s*$|\.style\.\w+\s*=\s*$|\.className\s*=\s*$|\.type\s*=\s*$|\.rel\s*=\s*$|\.href\s*=\s*$|"
    r"\.src\s*=\s*$|\.id\s*=\s*$|\.name\s*=\s*$|\.role\s*=\s*$|\.target\s*=\s*$|\.mode\s*=\s*$|"
    r"setAttribute\(\s*$|\.key\s*===?\s*$|toLocale\w*\(|Intl\.\w+\(|\bmethod\s*:\s*$|headers\s*:\s*\{[^}]*$)\s*$")
SETATTR_NAME = re.compile(r"setAttribute\(\s*$")
SETATTR_VALUE = re.compile(r"setAttribute\(\s*'([\w-]+)'\s*,\s*$")
# Code that names what the literal is for.
ROLE_CONTEXT = [
    ("toast", re.compile(r"(toast|notify|notice|\.toast)\??\.?\(\s*(\w+\s*,\s*)?$")),
    ("confirm", re.compile(r"(confirm|alert)\??\.?\(\s*$")),
    ("error", re.compile(r"(new Error|Refused|Exception)\(\s*$")),
    ("title", re.compile(r"\b(title|heading|caption|subject|helpSubject|shortTitle|displayName)\s*:\s*$")),
    ("label", re.compile(r"\b(label|labelShort|acceptLabel|cancelLabel|confirmLabel|menuLabel|text|word|name|"
                         r"placeholder|unit|summary)\s*:\s*$")),
    ("hint", re.compile(r"\b(hint|message|detail|description|headerCopy|headerCenter|copy|note|tooltip|intro|"
                        r"limitMessage|describe|reason|problem|empty)\s*:\s*$")),
    ("text-assignment", re.compile(r"\.(textContent|innerText|title|placeholder|ariaLabel|value)\s*=\s*$")),
    ("aria", re.compile(r"setAttribute\(\s*'(aria-(label|description|roledescription|valuetext|placeholder)|title|placeholder|alt)'\s*,\s*$")),
]
CRITICAL = re.compile(r"\b(wipe|erase|erases|reset database|cannot be undone|irreversib|destructive|permanent|"
                      r"delete[sd]?|remov(e|es|ed|ing)|clear(s|ed|ing)?\b|discard|revok|overwrit|restore|"
                      r"anyone who can reach|open to anyone|without a password|turns off password|"
                      r"private (network|lan)|local network|terminate|unsafe)", re.I)
NONHUMAN_TEXT = re.compile(
    r"^(\s*|[\w.\-]+:[\w.\-/]+|https?://\S+|/[\w./\-?=&%{}#:]*|[\w-]+\.(js|css|svg|png|json|html|ico)(\?\S*)?|"
    r"#[\w-]+|\.[\w-]+([.: #\[>][\w\-\[\]=\"'.:#()>* ]*)?|[a-z0-9_\-]+(\.[a-z0-9_\-]+)*|[A-Z0-9_]+|"
    r"[\w-]+/[\w.+-]+|\{\d+\}|[\d.,%:\- ]+(px|em|rem|ms|s|%|vh|vw|fr|deg)?|[-+*/=<>!&|^~?:;,.()\[\]{}\s\d]+|"
    r"(GET|PUT|POST|PATCH|DELETE|HEAD|OPTIONS)|[a-z]+(-[a-z]+)+|[a-z]+([A-Z][a-z]+)+|"
    r"(rgba?|hsla?|var|calc|url|linear-gradient|color-mix|cubic-bezier|steps)\(.*|\S*\$\{.*|"
    r"(?=.*-)[a-z0-9_-]+( [a-z0-9_-]+)+)$")                          # the last: a class list
CSS_LIKE = re.compile(r"^[\s\w\-.#:\[\]=\"'(),>*+~@%/]*\{|[\w-]+\s*:\s*[^;]+;|^@media|^[.#][\w-]")
SELECTOR_LIKE = re.compile(r"^[\w\-.#:\[\]=\"'(),>*+~ ]+$")
WORDS = re.compile(r"[A-Za-z]{2,}")


def stable_id(owner: str, context: str, text: str, ordinal: int) -> str:
    digest = hashlib.sha256(f"{owner}\0{context}\0{text}\0{ordinal}".encode()).hexdigest()[:12]
    return f"{Path(owner).stem}.{digest}"


def placeholders(text: str) -> list[str]:
    return sorted(set(re.findall(r"\{(\d+)\}", text)), key=int)


def looks_human(text: str, role: str | None) -> bool:
    t = text.strip()
    if not t or not WORDS.search(t):
        return False
    if t in CATALOG_KEYS:
        return False                                   # a key reference: the catalog's words
    if t.startswith(("<svg", "<path", "<circle", "<rect", "M ", "m ")) or "viewBox" in t:
        return False
    if t in ("use strict", "true", "false", "error", "warn", "info", "success", "warning"):
        return False
    bare = re.sub(r"\{\d+\}", "", t)
    if CSS_LIKE.search(bare) and not re.search(r"[a-z]{3,} [a-z]{3,}", bare.split("{")[0]):
        return False
    if role is None and NONHUMAN_TEXT.match(t):
        return False
    if role is None and " " not in t and not re.match(r"^[A-Z][a-z]+([A-Z]?[a-z]*)?[.!?…]?$|^[A-Z][a-z]+[A-Z][a-zA-Z]*$", t):
        # A lone lower-case or code-like token outside a labelling context.
        return False
    # A selector names things right after its marks (".row", "#id", "[x]", ":hover"); a sentence's
    # full stop is followed by a space or ends it.
    if role is None and SELECTOR_LIKE.match(t) and re.search(r"[.#][A-Za-z_-]|\[|:\w", t):
        return False
    return True


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Markup(HTMLParser):
    """Messages from markup: block text with inline markup kept, and human
    attributes. Text runs carry their {n} placeholders as they are. Text an
    element names a catalog key for (``data-i18n``) and the attributes it
    names keys for (``data-i18n-attr="attr:key;…"``) are keyed, not
    literals: they are left out. Each message carries the source line it
    starts on."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages: list[tuple[str, str, bool, int]] = []   # (kind, text, rich, line)
        self._buffer: list[str] = []
        self._rich = False
        self._line = None
        self._keyed = 0          # open elements inside a data-i18n element (0: not inside one)

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if text and WORDS.search(re.sub(r"<[^>]+>", "", text)):
            self.messages.append(("markup-text", text, self._rich, self._line))
        self._buffer, self._rich, self._line = [], False, None

    def handle_starttag(self, tag, attrs):
        if self._keyed:
            self._keyed += tag not in VOID_TAGS
            return
        named = dict(attrs)
        keyed_attributes = {pair.split(":", 1)[0].strip() for pair in (named.get("data-i18n-attr") or "").split(";")
                            if ":" in pair}
        for name, value in attrs:
            if name in keyed_attributes:
                continue
            if name in HUMAN_ATTRIBUTES and value and WORDS.search(value) and not NONHUMAN_TEXT.match(value.strip()):
                self.messages.append((f"attribute:{name}", value.strip(), False, self.getpos()[0]))
        if named.get("data-i18n") and tag not in VOID_TAGS:
            self._flush()
            self._keyed = 1
            return
        if tag in INLINE_TAGS:
            self._rich = self._rich or tag != "br"
            keep = [f'{n}="{v}"' for n, v in attrs if n in ("href", "target", "rel")]
            self._buffer.append(f"<{tag}{' ' + ' '.join(keep) if keep else ''}>")
        elif tag in BLOCK_TAGS or tag not in INLINE_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if self._keyed:
            self._keyed -= 1
            return
        if tag in INLINE_TAGS and tag != "br":
            self._buffer.append(f"</{tag}>")
        else:
            self._flush()

    def handle_data(self, data):
        if self._keyed:
            return
        if self._line is None and data.strip():
            self._line = self.getpos()[0]
        self._buffer.append(data)

    def close(self):
        super().close()
        self._flush()


def markup_messages(text: str) -> list[tuple[str, str, bool, int]]:
    parser = _Markup()
    parser.feed(text)
    parser.close()
    return parser.messages


def role_of(before: str) -> str | None:
    for name, pattern in ROLE_CONTEXT:
        if pattern.search(before):
            return name
    return None


def frontend_entries(path: Path) -> list[dict]:
    return frontend_entries_from(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())


def frontend_entries_from(source: str, owner: str) -> list[dict]:
    """The census of one script's source (or part of one)."""
    entries, ordinals = [], Counter()
    for lit in literals(source):
        before = source[max(0, lit.start - 160):lit.start]
        before_line = before.rsplit("\n", 1)[-1]
        if MACHINE_CONTEXT.search(before_line) or MACHINE_CONTEXT.search(before):
            if not SETATTR_VALUE.search(before_line):
                continue
        if SETATTR_VALUE.search(before_line):
            attribute = SETATTR_VALUE.search(before_line).group(1)
            # Only the attributes that carry text: an ARIA state or reference
            # (aria-haspopup, aria-controls, aria-current…) is a token.
            if attribute not in HUMAN_ATTRIBUTES + TEXT_ARIA:
                continue
        role = role_of(before_line)
        text = lit.text
        found = []
        if re.match(r"^\(?(min|max)-(width|height)|^(Arrow\w+|Escape|Enter|Tab|Home|End|PageUp|PageDown|Backspace)$|^-{3,}", text.strip()):
            continue
        if re.search(r"(^|[\s,(])[\w*]*\[[\w-]+(=|\])|:not\(", text) and "<" not in text:
            continue                                   # a CSS selector
        if re.match(r'^\s*[\w-]+="', text) and "<" not in text.split('="', 1)[0]:
            text = "<x " + text + ("" if ">" in text else ">")   # attributes of an element built in parts
        if re.search(r"<[a-zA-Z][\w-]*[\s>/]|</[a-zA-Z]|=\"|^\s*\"?>", text):
            # Markup, or a fragment of markup joined to its neighbours.
            text = re.sub(r'^\s*"?\s*>', "", text)
            text = re.sub(r'(^|\s)[\w-]+="[^"]*$|^[^<>"]*"(?=\s|>|$)', " ", text)
            for kind, message, rich, _line in markup_messages(text):
                if looks_human(re.sub(r"<[^>]+>", "", message), role or "markup"):
                    found.append((kind, message, rich))
        elif looks_human(text, role):
            found.append((role or "literal", re.sub(r"\s+", " ", text).strip(), False))
        for kind, message, rich in found:
            context = kind
            key = (owner, context, message)
            ordinals[key] += 1
            entries.append({
                "id": stable_id(owner, context, message, ordinals[key]),
                "english": message,
                "owner": owner,
                "line": lit.line,
                "context": context,
                "literal": lit.kind,
                "rich": rich,
                "placeholders": placeholders(message),
                "substitutions": lit.substitutions if placeholders(message) else [],
                "criticality": "critical-warning" if CRITICAL.search(message) and len(message) > 12 else "normal",
            })
    return entries


def html_entries(path: Path) -> list[dict]:
    owner = path.relative_to(ROOT).as_posix()
    source = path.read_text(encoding="utf-8")
    # Scripts, styles, icons and comments are dropped, their lines kept, so a
    # message's line is its line in the file.
    body = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>|<!--.*?-->",
                  lambda m: "\n" * m.group(0).count("\n"), source, flags=re.S)
    entries, ordinals = [], Counter()
    for kind, message, rich, line in markup_messages(body):
        if not looks_human(re.sub(r"<[^>]+>", "", message), "markup"):
            continue
        ordinals[(kind, message)] += 1
        entries.append({
            "id": stable_id(owner, kind, message, ordinals[(kind, message)]), "english": message, "owner": owner,
            "line": line, "context": kind, "literal": "html",
            "rich": rich, "placeholders": [], "substitutions": [],
            "criticality": "critical-warning" if CRITICAL.search(message) and len(message) > 12 else "normal",
        })
    return entries


SERVER_DETAIL = re.compile(
    r"(?:HTTPException\([^)]*?detail\s*=\s*|HTTPException\(\s*(?:status_code\s*=\s*)?[\w.]+\s*,\s*"
    r"|raise\s+(\w*(?:Refused|Rejected|Invalid\w*|Error|Busy|Stale)\w*)\(\s*(?:[\w.]+,\s*)?)"
    r"(f?)(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')", re.S)
# A structured refusal's own words, in the routes and the application's handlers.
STRUCTURED_DETAIL = re.compile(r"[\"'](?:message|detail)[\"']\s*:\s*(f?)(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')")


def _call_span(source: str, start: int) -> str:
    """The text of the call an entry's string sits in, to its closing parenthesis."""
    depth, i = 1, start
    while i < len(source) and depth:
        ch = source[i]
        if ch in "\"'":
            quote, i = ch, i + 1
            while i < len(source) and source[i] != quote and source[i] != "\n":
                i += 2 if source[i] == "\\" else 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return source[start:i]


def backend_entries() -> list[dict]:
    entries = []
    for path in sorted(BACKEND.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        owner = path.relative_to(ROOT).as_posix()
        ordinals = Counter()
        found = [(match, match.group(1) or "HTTPException", match.group(2), match.group(3))
                 for match in SERVER_DETAIL.finditer(source)]
        if owner.startswith("backend/api/") or owner == "backend/main.py":
            found += [(match, "structured", match.group(1), match.group(2)) for match in STRUCTURED_DETAIL.finditer(source)]
        for match, kind, formatted, quoted in sorted(found, key=lambda item: item[0].start()):
            text = quoted[1:-1]
            if not WORDS.search(text) or not re.search(r"[A-Za-z]{3,} ", text):
                continue
            message = re.sub(r"\{[^}]*\}", lambda m, c=iter(range(99)): "{%d}" % next(c), text) if formatted else text
            ordinals[message] += 1
            span = _call_span(source, match.end())
            if kind == "structured":
                # The dict the words sit in names its own code.
                opening = source.rfind("{", 0, match.start())
                coded = bool(re.search(r"[\"']code[\"']\s*:", source[opening:match.start()] + span))
            else:
                coded = bool(re.search(r"\bcode\s*=(?!=)", span))
            entries.append({
                "id": stable_id(owner, "server-detail", message, ordinals[message]), "english": message,
                "owner": owner, "line": source.count("\n", 0, match.start()) + 1, "context": "server-detail",
                "literal": "python", "rich": False, "placeholders": placeholders(message), "substitutions": [],
                "criticality": "critical-warning" if CRITICAL.search(message) and len(message) > 12 else "normal",
                "exception": kind, "coded": coded,
            })
    return entries


def census() -> dict:
    entries = []
    for path in sorted(STATIC.glob("*.js")):
        entries.extend(frontend_entries(path))
    for path in sorted(STATIC.glob("*.html")):
        entries.extend(html_entries(path))
    server = backend_entries()
    reviewed = json.loads(EXEMPTIONS.read_text(encoding="utf-8"))["exempt"] if EXEMPTIONS.is_file() else []
    reasons = {(item["owner"], item["english"]): item["reason"] for item in reviewed}
    exempt = [{**entry, "reason": reasons[(entry["owner"], entry["english"])]}
              for entry in entries if (entry["owner"], entry["english"]) in reasons]
    entries = [entry for entry in entries if (entry["owner"], entry["english"]) not in reasons]
    # A server message is coded at its raise, exempt with a reason, or outstanding.
    for entry in server:
        entry["reason"] = reasons.get((entry["owner"], entry["english"]), "") if not entry["coded"] else ""
    return {"schema": "debridpulse-localization-inventory/1", "entries": entries, "exempt": exempt, "server": server}


def summarize(data: dict) -> dict:
    entries, server = data["entries"], data["server"]
    by_owner = Counter(e["owner"] for e in entries)
    by_context = Counter(e["context"].split(":")[0] for e in entries)
    return {
        "frontend_entries": len(entries),
        "exempt": len(data.get("exempt", [])),
        "frontend_unique_english": len({e["english"] for e in entries}),
        "rich_text": sum(e["rich"] for e in entries),
        "with_placeholders": sum(bool(e["placeholders"]) for e in entries),
        "critical_warning": sum(e["criticality"] == "critical-warning" for e in entries),
        "help_long_form": by_owner["frontend/static/ui-help-page.js"] + by_owner["frontend/static/ui-settings-help.js"],
        "server_detail_entries": len(server),
        "server_coded": sum(e["coded"] for e in server),
        "server_exempt": sum(bool(e.get("reason")) for e in server),
        "server_outstanding": sum(not e["coded"] and not e.get("reason") for e in server),
        "by_owner": dict(by_owner.most_common()),
        "by_context": dict(by_context.most_common()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    data = census()
    if args.json:
        Path(args.json).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.summary or not args.json:
        print(json.dumps(summarize(data), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
