"""A small JavaScript lexer for DebridPulse's localization census.

It does one thing: find every string literal and template literal in a
first-party script, with its exact position, the text of each template
chunk (``${…}`` substitutions become ``{0}``, ``{1}``…), and enough of the
preceding source to classify what the text is for. It understands comments,
single/double/template strings (including nested templates inside
substitutions), and regular-expression literals (by the token that precedes a
``/``). It is not a parser and never evaluates anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# A '/' after one of these (or at the start) begins a regular expression.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%<>~^") | {""}
_REGEX_KEYWORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else"}


@dataclass
class Literal:
    kind: str                 # 'string' | 'template'
    text: str                 # the literal's text; template substitutions as {0}, {1}…
    start: int                # offset of the opening quote
    end: int                  # offset just past the closing quote
    line: int
    substitutions: list[str] = field(default_factory=list)   # source of each ${…}


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _unescape(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            simple = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\", "`": "`", "$": "$", "0": "\0"}
            if nxt in simple:
                out.append(simple[nxt]); i += 2; continue
            if nxt == "u" and raw[i + 2:i + 3] == "{":
                close = raw.index("}", i)
                out.append(chr(int(raw[i + 3:close], 16))); i = close + 1; continue
            if nxt == "u":
                out.append(chr(int(raw[i + 2:i + 6], 16))); i += 6; continue
            if nxt == "x":
                out.append(chr(int(raw[i + 2:i + 4], 16))); i += 4; continue
            if nxt == "\n":
                i += 2; continue
            out.append(nxt); i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


class _Lexer:
    def __init__(self, source: str):
        self.s = source
        self.i = 0
        self.literals: list[Literal] = []
        self.last = ""          # last significant token (punctuator char or word)

    def run(self) -> list[Literal]:
        self._scan(until=None)
        return self.literals

    def _scan(self, until: str | None) -> None:
        """Scan code; when `until` is '}', stop at the brace closing a template
        substitution (tracking nested braces)."""
        s, depth = self.s, 0
        while self.i < len(s):
            ch = s[self.i]
            if ch in " \t\r\n":
                self.i += 1; continue
            if s.startswith("//", self.i):
                self.i = s.find("\n", self.i) if s.find("\n", self.i) >= 0 else len(s); continue
            if s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                self.i = len(s) if end < 0 else end + 2; continue
            if ch in "'\"":
                self._string(ch); self.last = "str"; continue
            if ch == "`":
                self._template(); self.last = "str"; continue
            if ch == "/":
                if self.last in _REGEX_PRECEDERS or self.last in _REGEX_KEYWORDS:
                    self._regex(); self.last = "re"; continue
                self.i += 1; self.last = "/"; continue
            if until == "}" and ch == "{":
                depth += 1
            if until == "}" and ch == "}":
                if depth == 0:
                    return
                depth -= 1
            if ch.isalnum() or ch in "_$":
                j = self.i
                while j < len(s) and (s[j].isalnum() or s[j] in "_$"):
                    j += 1
                self.last = s[self.i:j]; self.i = j; continue
            self.last = ch if ch in _REGEX_PRECEDERS or ch in ")]}" else ch
            if ch in ")]":
                self.last = ")"          # a '/' after ')' or ']' divides
            self.i += 1

    def _string(self, quote: str) -> None:
        s, start = self.s, self.i
        j = start + 1
        while j < len(s) and s[j] != quote:
            if s[j] == "\\":
                j += 2; continue
            if s[j] == "\n":
                break
            j += 1
        raw = s[start + 1:j]
        self.literals.append(Literal("string", _unescape(raw), start, j + 1, _line(s, start)))
        self.i = j + 1

    def _template(self) -> None:
        s, start = self.s, self.i
        j = start + 1
        parts, subs = [], []
        chunk_start = j
        while j < len(s):
            if s[j] == "\\":
                j += 2; continue
            if s[j] == "`":
                parts.append(s[chunk_start:j]); break
            if s.startswith("${", j):
                parts.append(s[chunk_start:j])
                self.i = j + 2
                sub_start = self.i
                self._scan(until="}")
                subs.append(s[sub_start:self.i].strip())
                j = self.i + 1
                chunk_start = j
                parts.append(None)          # marks a substitution
                continue
            j += 1
        text, n = [], 0
        for part in parts:
            if part is None:
                text.append("{%d}" % n); n += 1
            else:
                text.append(_unescape(part))
        self.literals.append(Literal("template", "".join(text), start, j + 1, _line(s, start), subs))
        self.i = j + 1

    def _regex(self) -> None:
        s, j, in_class = self.s, self.i + 1, False
        while j < len(s):
            ch = s[j]
            if ch == "\\":
                j += 2; continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                break
            elif ch == "\n":
                break
            j += 1
        j += 1
        while j < len(s) and s[j].isalpha():
            j += 1
        self.i = j


def literals(source: str) -> list[Literal]:
    return _Lexer(source).run()
