#!/usr/bin/env python3
"""Create or update a public GitHub release from the matching changelog entry.

Usage: python3 release.py <version>  (for example: python3 release.py 1.0.0)

The body is exactly the version's own changelog entry: from its ``## [x.y.z]``
heading at the start of a line up to the next such heading. An entry still
titled ``Development`` is not a release, and a body longer than GitHub accepts
is refused before anything is sent -- never truncated. Any failed GitHub call
stops the script with a non-zero exit instead of reporting success.
"""
import sys, re, json, subprocess, pathlib, os

# GitHub refuses a release body longer than this many characters.
MAX_BODY_CHARS = 125_000


def extract_entry(changelog: str, version: str) -> str | None:
    """The version's own entry, or None. Only a heading at the start of a line
    opens or closes an entry, so a heading quoted in another entry's text,
    a deeper heading, or a version that merely starts with this one never
    matches."""
    m = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*(?:\n(?!## \[)[^\n]*)*",
        changelog, re.MULTILINE
    )
    return m.group().strip() if m else None


def fail(message: str) -> None:
    print(f"ERROR: {message}")
    sys.exit(1)


def api(method: str, url: str, headers: list[str], payload: str | None = None) -> tuple[int, dict]:
    """One GitHub call: its HTTP status and JSON answer. The payload goes
    through stdin, so its size is never bounded by the argument limit."""
    data = ["--data-binary", "@-"] if payload is not None else []
    r = subprocess.run(
        ["curl", "-sS", "-X", method, "-w", "\n%{http_code}"] + headers + data + [url],
        input=payload, capture_output=True, text=True
    )
    if r.returncode != 0:
        fail(f"{method} {url} fehlgeschlagen (curl {r.returncode}): {r.stderr.strip()}")
    text, _, status = r.stdout.rpartition("\n")
    try:
        return int(status), (json.loads(text) if text.strip() else {})
    except ValueError:
        fail(f"{method} {url}: unerwartete Antwort (HTTP {status or '?'})")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 release.py <version>")
        sys.exit(1)

    version = sys.argv[1].lstrip('v')
    tag = f"v{version}"

    # 1. CHANGELOG-Eintrag extrahieren
    changelog = pathlib.Path("CHANGELOG.md").read_text()
    body = extract_entry(changelog, version)
    if body is None:
        fail(f"[{version}] nicht in CHANGELOG.md gefunden!")
    title = body.splitlines()[0]
    if "development" in title.casefold():
        fail(f"[{version}] ist noch ein Development-Eintrag, kein Release: {title}")
    print(f"Changelog-Eintrag: {len(body)} Zeichen")
    if len(body) > MAX_BODY_CHARS:
        fail(f"[{version}] hat {len(body)} Zeichen; GitHub erlaubt hoechstens {MAX_BODY_CHARS}. "
             "Nichts wurde gesendet und nichts gekuerzt.")

    # 2. PAT aus Umgebung
    pat = os.environ.get("GITHUB_PAT", "")
    if not pat:
        fail("GITHUB_PAT Umgebungsvariable nicht gesetzt!")

    headers = ["-H", f"Authorization: token {pat}", "-H", "Content-Type: application/json"]
    repo = "https://api.github.com/repos/Xipher-Zero/debridpulse"

    # 3. Existierendes Release holen: 404 heisst "noch keines", alles andere ausser 200 ist ein Fehler
    status, existing = api("GET", f"{repo}/releases/tags/{tag}", headers)
    if status == 200 and existing.get("id"):
        release_id = existing["id"]
    elif status == 404:
        release_id = None
    else:
        fail(f"Release {tag} konnte nicht gelesen werden (HTTP {status}): {existing.get('message', '')}")

    payload = json.dumps({"tag_name": tag, "name": tag, "body": body})

    if release_id:
        # Update
        status, resp = api("PATCH", f"{repo}/releases/{release_id}", headers, payload)
        expected, action = 200, "aktualisiert"
    else:
        # Neu erstellen
        status, resp = api("POST", f"{repo}/releases", headers, payload)
        expected, action = 201, "erstellt"
    if status != expected or not resp.get("html_url"):
        fail(f"Release {tag} wurde nicht {action} (HTTP {status}): "
             f"{resp.get('message', '')} {json.dumps(resp.get('errors', ''))}")
    print(f"Release {tag} {action}: {resp.get('html_url')}")


if __name__ == "__main__":
    main()
