from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


SHELL_SELECTOR = re.compile(
    r"body(?:\.light)?\.dp-v11-structural\s+#(?P<node>sidebar|main)\s*\{(?P<body>[^}]*)\}",
    re.DOTALL,
)


def test_dashboard_review_layers_cannot_own_outer_shell_radius_or_shadow():
    """Page-review CSS must not paint the application shell boundary."""
    offenders = []
    for path in sorted(STATIC.glob("ui-dashboard*.css")):
        css = path.read_text(encoding="utf-8")
        for match in SHELL_SELECTOR.finditer(css):
            body = match.group("body")
            forbidden = [token for token in ("box-shadow", "border-radius") if token in body]
            if forbidden:
                offenders.append((path.name, match.group("node"), forbidden))

    assert offenders == [], f"Dashboard CSS leaked shell material: {offenders}"


def test_shared_shell_owns_a_straight_sidebar_canvas_seam():
    css = read_static("ui-shell-structural.css")
    selector = "body.dp-v11-structural #sidebar"
    assert selector in css
    rule = css.split(selector, 1)[1].split("}", 1)[0]

    assert "border-right:" in rule
    assert "box-shadow: none;" in rule
    assert "border-radius" not in rule


def test_settings_page_never_paints_the_outer_shell_seam():
    css = read_static("ui-settings-page.css")
    assert "#sidebar" not in css
    assert "body.dp-v11-structural #main" not in css
