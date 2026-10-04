"""Browser Runtime test isolation: one owning spec FILE per shared settings key.

Playwright runs spec files concurrently against ONE shared backend. A settings
key that two files mutate -- or that one file mutates while another asserts its
live value -- is a race between files, and the loser reports a persistence
defect against innocent code. ``integrations.usenet.enabled`` had four such
files, and Browser Runtime twice failed on it with opposite expectations.

The contract: for every guarded key, exactly ONE spec file mutates or asserts
the live value. Every other spec that needs that state renders an injected
settings document (the ``settings-field-geometry.spec.js`` pattern) and never
touches the shared key.

The analysis is static and deliberately conservative: a write or live-read
site whose integration id is a template variable counts for every id the file
can feed into that variable (a ``for ... of`` array, a named array, the keys of
an object iterated with ``Object.entries``/``Object.keys``, a string literal
passed to the helper that contains the site, or a string constant it names). Over-approximation can only
report a file as a dependent, never hide one.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

SPEC_DIR = Path(__file__).resolve().parents[2] / "frontend" / "browser"

# Shared settings key -> its one owning spec file. ``integrations.<id>.enabled``
# is guarded per integration id.
OWNERS = {
    "usenet": "usenet-server-cards.spec.js",
    # The file whose credential and card tests depend on AllDebrid's live
    # state; its own cases run serially, so its Enable round-trip cannot race
    # them. Three files used to flip this key concurrently.
    "alldebrid": "settings-providers-persistence.spec.js",
    # The Network Sources master file, whose aggregate and member cases need
    # every member's live state; four other files used to flip HTTP(S), and
    # one more raced it on (S)FTP.
    "general_http": "general-sources-master.spec.js",
    "general_ftp": "general-sources-master.spec.js",
}

# Spec files that serve EVERY settings document they render themselves: their
# reads of ``integrations.<id>.enabled`` read their own fixture, never the shared
# backend. Enforced by ``test_an_injected_document_spec_never_reaches_the_shared_settings``;
# their writes (if any) still count.
INJECTED_DOCUMENT_SPECS = {"ui-fix-ws1-p2.spec.js"}
# Spec files that run against their OWN candidate server (named by the
# environment variable) and never address the shared backend, so nothing they
# do to a settings key can race another file. Enforced by
# ``test_an_own_backend_spec_never_reaches_the_shared_backend``.
OWN_BACKEND_SPECS = {"usenet-live-provider-status.spec.js": "DP_USENET_BASE_URL"}

_ID = r"[a-z0-9_]+"
_VAR = r"[A-Za-z_$][\w$]*"


def _strip_comments(source: str) -> str:
    """Blank every comment while keeping string and template literals intact."""
    out, index, length = [], 0, len(source)
    while index < length:
        char = source[index]
        pair = source[index:index + 2]
        if pair == "//":
            end = source.find("\n", index)
            end = length if end < 0 else end
            out.append(" " * (end - index))
            index = end
        elif pair == "/*":
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", source[index:end]))
            index = end
        elif char in "'\"`":
            end = index + 1
            while end < length and source[end] != char:
                end += 2 if source[end] == "\\" else 1
            out.append(source[index:end + 1])
            index = end + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _balanced(source: str, start: int) -> str:
    """The text inside the bracket opening at ``start`` (exclusive)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack, index = [pairs[source[start]]], start + 1
    while index < len(source) and stack:
        char = source[index]
        if char in "'\"`":
            end = index + 1
            while end < len(source) and source[end] != char:
                end += 2 if source[end] == "\\" else 1
            index = end
        elif char in pairs:
            stack.append(pairs[char])
        elif char == stack[-1]:
            stack.pop()
        index += 1
    return source[start + 1:index - 1]


def _top_level(text: str) -> list[str]:
    """Comma-separated items of a bracket body, split at depth zero."""
    items, depth, current, quote = [], 0, [], None
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if "".join(current).strip():
        items.append("".join(current).strip())
    return items


def _literal(item: str) -> str | None:
    match = re.fullmatch(r"(['\"`])(" + _ID + r")\1", item.strip())
    return match.group(2) if match else None


class _Spec:
    def __init__(self, source: str):
        self.code = _strip_comments(source)
        self.functions = {}
        for match in re.finditer(r"(?:async\s+)?function\s+(" + _VAR + r")\s*\(", self.code):
            self.functions[match.group(1)] = _top_level(_balanced(self.code, match.end() - 1))
        for match in re.finditer(r"\b(?:const|let)\s+(" + _VAR + r")\s*=\s*(?:async\s*)?\(", self.code):
            opening = match.end() - 1
            params = _balanced(self.code, opening)
            after = self.code[opening + len(params) + 2:].lstrip()
            if after.startswith("=>"):
                self.functions[match.group(1)] = _top_level(params)
        for match in re.finditer(r"\b(?:const|let)\s+(" + _VAR + r")\s*=\s*(?:async\s+)?(" + _VAR + r")\s*=>",
                                 self.code):
            self.functions[match.group(1)] = [match.group(2)]

    def _array(self, name: str) -> set[str]:
        values = set()
        for match in re.finditer(r"\b(?:const|let)\s+" + re.escape(name) + r"\s*=\s*\[", self.code):
            values |= {value for value in map(_literal, _top_level(_balanced(self.code, match.end() - 1))) if value}
        return values

    def _object_keys(self, name: str) -> set[str]:
        keys = set()
        for match in re.finditer(r"\b" + re.escape(name) + r"\s*=\s*\{", self.code):
            for item in _top_level(_balanced(self.code, match.end() - 1)):
                key = re.match(r"(['\"]?)(" + _ID + r")\1\s*:", item)
                if key:
                    keys.add(key.group(2))
        return keys

    def _iterable(self, expression: str) -> set[str]:
        expression = expression.strip()
        if expression.startswith("["):
            return {value for value in map(_literal, _top_level(_balanced(expression, 0))) if value}
        match = re.fullmatch(r"Object\.(?:entries|keys)\((" + _VAR + r")\)", expression)
        if match:
            return self._object_keys(match.group(1))
        if re.fullmatch(_VAR, expression):
            return self._array(expression)
        return set()

    def resolve(self, name: str, depth: int = 0) -> set[str]:
        """Every integration id this file can feed into variable ``name``."""
        if depth > 4:
            return set()
        values = set()
        for match in re.finditer(r"\b(?:const|let)\s+" + re.escape(name) + r"\s*=\s*(['\"`])(" + _ID + r")\1",
                                 self.code):
            values.add(match.group(2))
        loop = r"for\s*\(\s*(?:const|let)\s+(?:\[\s*)?" + re.escape(name) + r"\b[^;]*?\bof\s+"
        for match in re.finditer(loop, self.code):
            start = match.end()
            if self.code[start] == "[":
                values |= self._iterable("[" + _balanced(self.code, start) + "]")
            else:
                call = re.match(r"Object\.(?:entries|keys)\(\s*" + _VAR + r"\s*\)|" + _VAR, self.code[start:])
                if call:
                    values |= self._iterable(call.group(0))
        for function, params in self.functions.items():
            if name not in params:
                continue
            position = params.index(name)
            for call in re.finditer(r"(?<![\w$.])" + re.escape(function) + r"\s*\(", self.code):
                arguments = _top_level(_balanced(self.code, call.end() - 1))
                if position >= len(arguments):
                    continue
                argument = arguments[position]
                literal = _literal(argument)
                if literal:
                    values.add(literal)
                elif re.fullmatch(_VAR, argument) and argument != name:
                    values |= self.resolve(argument, depth + 1)
                elif argument == name:
                    continue
        return values

    def enabled_sites(self) -> tuple[set[str], set[str]]:
        """(ids this file writes, ids whose live value this file asserts)."""
        writes, reads = set(), set()
        # Operating an Enable toggle writes it (the toggle IS the commit).
        for match in re.finditer(r"dp-settings-integration-(" + _ID + r")-enabled", self.code):
            writes.add(match.group(1))
        for match in re.finditer(r"dp-settings-integration-\$\{(" + _VAR + r")\}-enabled", self.code):
            writes |= self.resolve(match.group(1))
        # A scoped configuration PATCH that carries ``enabled``.
        for match in re.finditer(r"\.patch\(\s*(['\"`])/api/integrations/(?:(" + _ID + r")|\$\{(" + _VAR
                                 + r")\})/configuration\1", self.code):
            body = _balanced(self.code, self.code.index("(", match.start()))
            if not re.search(r"\benabled\b", body):
                continue
            writes |= {match.group(2)} if match.group(2) else self.resolve(match.group(3))
        # Asserting the shared document's live value.
        for match in re.finditer(r"integrations(?:\?\.|\.)(" + _ID + r")\??\.enabled", self.code):
            reads.add(match.group(1))
        for match in re.finditer(r"integrations\??\.?\[\s*(?:(['\"])(" + _ID + r")\1|(" + _VAR + r"))\s*\]\??\.enabled",
                                 self.code):
            reads |= {match.group(2)} if match.group(2) else self.resolve(match.group(3))
        return writes, reads


def _dependents(spec_dir: Path = SPEC_DIR) -> dict[str, dict[str, set[str]]]:
    """{integration id: {spec file: {"writes"/"reads"}}} across the suite."""
    found: dict[str, dict[str, set[str]]] = {}
    for path in sorted(spec_dir.glob("*.spec.js")):
        if path.name in OWN_BACKEND_SPECS:
            continue
        writes, reads = _Spec(path.read_text(encoding="utf-8")).enabled_sites()
        if path.name in INJECTED_DOCUMENT_SPECS:
            reads = set()
        for identity in writes | reads:
            kinds = found.setdefault(identity, {}).setdefault(path.name, set())
            kinds |= {"writes"} if identity in writes else set()
            kinds |= {"reads"} if identity in reads else set()
    return found


def _assert_one_owner(identity: str, dependents: dict[str, set[str]]) -> None:
    owner = OWNERS[identity]
    assert owner in dependents and "writes" in dependents[owner], (
        f"{owner} no longer owns integrations.{identity}.enabled; move the ownership, do not drop it")
    assert set(dependents) == {owner}, (
        f"integrations.{identity}.enabled is mutated or asserted live by more than one spec file: "
        f"{ {name: sorted(kinds) for name, kinds in sorted(dependents.items())} }. Spec files share one "
        f"backend and run concurrently; only {owner} may touch it, others render an injected settings document")


@pytest.mark.parametrize("identity", sorted(OWNERS))
def test_a_guarded_integration_enabled_key_has_exactly_one_owning_spec_file(identity):
    _assert_one_owner(identity, _dependents().get(identity, {}))


@pytest.mark.parametrize("identity", ["general_http", "general_ftp"])
@pytest.mark.parametrize("intruder", [
    "await page.locator('label[for=\"dp-settings-integration-{id}-enabled\"]').click();",
    "const PROVIDER = '{id}';\n"
    "await request.patch(`/api/integrations/${{PROVIDER}}/configuration`, {{data: {{enabled: false}}}});",
])
def test_a_second_writer_of_a_guarded_key_is_named(tmp_path, identity, intruder):
    owner = OWNERS[identity]
    (tmp_path / owner).write_text((SPEC_DIR / owner).read_text(encoding="utf-8"), encoding="utf-8")
    _assert_one_owner(identity, _dependents(tmp_path)[identity])
    (tmp_path / "intruder.spec.js").write_text(intruder.format(id=identity), encoding="utf-8")
    with pytest.raises(AssertionError, match=r"'intruder\.spec\.js': \['writes'\]"):
        _assert_one_owner(identity, _dependents(tmp_path)[identity])


@pytest.mark.parametrize("name", sorted(INJECTED_DOCUMENT_SPECS))
def test_an_injected_document_spec_never_reaches_the_shared_settings(name):
    code = _strip_comments((SPEC_DIR / name).read_text(encoding="utf-8"))
    # It fulfils the settings document from its own state...
    route = re.search(r"page\.route\(\s*'\*\*/api/settings'\s*,", code)
    assert route, f"{name} does not serve its own settings document"
    handler = _balanced(code, code.index("(", route.start()))
    assert "route.fulfill(" in handler and "route.fetch(" not in handler and "route.continue(" not in handler
    # ...answers every integration configuration write itself...
    assert re.search(r"page\.route\(/\\/api\\/integrations\\/", code), f"{name} lets integration writes through"
    # ...and never talks to the shared backend's settings directly.
    assert not re.search(r"\brequest\.(?:get|patch|put|post)\(", code), f"{name} reads or writes live settings"


@pytest.mark.parametrize("name", sorted(OWN_BACKEND_SPECS))
def test_an_own_backend_spec_never_reaches_the_shared_backend(name):
    code = _strip_comments((SPEC_DIR / name).read_text(encoding="utf-8"))
    assert f"const BASE = process.env.{OWN_BACKEND_SPECS[name]};" in code
    # No shared-backend fixture, and every navigation and request names BASE.
    assert not re.search(r"\(\{[^}]*\brequest\b[^}]*\}\)", code), f"{name} takes the shared request fixture"
    calls = list(re.finditer(r"\bpage\.(?:goto|request\.(?:get|post|put|patch|delete|fetch))\(", code))
    assert calls, f"{name} reaches no backend at all"
    for call in calls:
        argument = _top_level(_balanced(code, call.end() - 1))[0]
        assert argument.startswith("`${BASE}/"), f"{name} addresses a backend other than its own: {argument}"


@pytest.mark.parametrize("source, writes, reads", [
    ("page.locator('label[for=\"dp-settings-integration-usenet-enabled\"]').click();", {"usenet"}, set()),
    ("await request.patch('/api/integrations/usenet/configuration', {data: {enabled: true}});", {"usenet"}, set()),
    ("await request.patch('/api/integrations/usenet/configuration', {data: {options: {}}});", set(), set()),
    ("const T = ['usenet', 'other'];\nasync function flip(page, id) {\n"
     "  await page.locator(`label[for=\"dp-settings-integration-${id}-enabled\"]`).click();\n}\n"
     "for (const id of T) { test(id, async ({page}) => { await flip(page, id); }); }",
     {"usenet", "other"}, set()),
    ("for (const id of ['alldebrid', 'usenet']) {\n"
     "  await request.patch(`/api/integrations/${id}/configuration`, {data: {enabled: true}});\n}",
     {"alldebrid", "usenet"}, set()),
    ("original = {usenet: true};\nfor (const [id, enabled] of Object.entries(original)) {\n"
     "  await request.patch(`/api/integrations/${id}/configuration`, {data: {enabled}});\n}", {"usenet"}, set()),
    ("const persisted = async (page, id) => (await get()).integrations[id]?.enabled;\n"
     "await persisted(page, 'usenet');", set(), {"usenet"}),
    ("expect(settings.integrations.usenet.enabled).toBe(true);", set(), {"usenet"}),
    # A string constant named in the scoped path is the id it carries.
    ("const PROVIDER = 'general_http';\n"
     "await request.patch(`/api/integrations/${PROVIDER}/configuration`, {data: {enabled: true}});",
     {"general_http"}, set()),
    # Naming an id elsewhere (an expected set of rendered controls) feeds no site.
    ("const label = id => `label[for=\"dp-settings-integration-${id}-enabled\"]`;\nlabel('general_http');\n"
     "expect(new Set(controls)).toEqual(new Set(['usenet']));", {"general_http"}, set()),
    # Comments are not code.
    ("// label[for=\"dp-settings-integration-usenet-enabled\"]\n/* integrations.usenet.enabled */", set(), set()),
])
def test_the_ownership_analysis_recognizes_every_write_and_live_read_form(source, writes, reads):
    assert _Spec(source).enabled_sites() == (writes, reads)
