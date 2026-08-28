from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read_static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


SHELL_SELECTOR = re.compile(
    r"body(?:\.light)?\.dp-v11-structural\s+#(?P<node>sidebar|main)(?P<pseudo>::(?:before|after))?\s*\{(?P<body>[^}]*)\}",
    re.DOTALL,
)
IMPORT_SELECTOR = re.compile(r"@import\s+url\('/(?P<name>[^'?]+)\?[^']+'\);")
CSS_RULE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", re.DOTALL)
CARD_ALIASES = ("card", "dp-card", "scard", "list-card")
SHELL_ROOTS = ("sidebar", "main", "topbar", "content")
CARD_SHELL_GUARD = ":not(#sidebar):not(#main):not(#topbar):not(#content)"


def test_dashboard_review_layers_cannot_own_outer_shell_radius_or_shadow():
    """Page-review CSS must not paint the application shell boundary."""
    offenders = []
    for path in sorted(STATIC.glob("ui-dashboard*.css")):
        css = path.read_text(encoding="utf-8")
        for match in SHELL_SELECTOR.finditer(css):
            body = match.group("body")
            forbidden = [token for token in ("box-shadow", "border-radius") if token in body]
            if forbidden:
                offenders.append((path.name, match.group("node"), match.group("pseudo"), forbidden))

    assert offenders == [], f"Dashboard CSS leaked shell material: {offenders}"


def test_post_structural_layers_cannot_repaint_outer_shell_seam():
    """Once the structural shell is loaded, later CSS cannot reacquire its seam."""
    imports = IMPORT_SELECTOR.findall(read_static("style-v11.css"))
    structural_index = imports.index("ui-shell-structural.css")
    late_styles = imports[structural_index + 1 :]

    # ui-shell-runtime.js appends this stylesheet after the parser-loaded stack.
    late_styles.append("ui-shell-brand.css")

    offenders = []
    for name in late_styles:
        css = read_static(name)
        for rule in CSS_RULE.finditer(css):
            body = rule.group("body")
            forbidden = [token for token in ("box-shadow", "border-radius") if token in body]
            if not forbidden:
                continue

            for raw_selector in rule.group("selectors").split(","):
                selector = " ".join(raw_selector.split())
                for node in ("sidebar", "main"):
                    if re.search(rf"#{node}(?:::(?:before|after))?\s*$", selector):
                        offenders.append((name, selector, forbidden))

    assert offenders == [], f"Late CSS reacquired outer shell material: {offenders}"


def test_universal_card_bridge_cannot_paint_shell_roots():
    """Legacy card aliases must never be able to convert shell canvases into cards."""
    css = read_static("ui-universal-language.css")
    aliases = ":is(.dp-card, .card, .scard, .list-card)"
    guarded = f"body.dp-v11-structural {aliases}{CARD_SHELL_GUARD}"

    assert f"{guarded} {{" in css
    assert f"{guarded}::after {{" in css
    assert f"body.dp-v11-structural {aliases} {{" not in css
    assert f"body.dp-v11-structural {aliases}::after {{" not in css

    material_rule = css.split(f"{guarded} {{", 1)[1].split("}", 1)[0]
    assert "border-radius: var(--dp-radius-lg);" in material_rule
    assert "background: var(--dp-panel-surface);" in material_rule
    assert "box-shadow: var(--dp-panel-shadow);" in material_rule

    frame_rule = css.split(f"{guarded}::after {{", 1)[1].split("}", 1)[0]
    assert "border: 1px solid var(--dp-panel-frame);" in frame_rule
    assert "mask-image:" in frame_rule


def test_static_shell_roots_do_not_carry_card_aliases():
    """The static application shell must remain structural before any runtime executes."""
    index = read_static("index.html")
    for root in SHELL_ROOTS:
        match = re.search(rf"<[^>]+\bid=[\"']{root}[\"'][^>]*>", index)
        assert match, f"Missing static shell root #{root}"
        tag = match.group(0)
        class_match = re.search(r"\bclass=[\"']([^\"']*)[\"']", tag)
        classes = set(class_match.group(1).split()) if class_match else set()
        leaked = classes.intersection(CARD_ALIASES)
        assert not leaked, f"#{root} carries card aliases in static markup: {sorted(leaked)}"


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
