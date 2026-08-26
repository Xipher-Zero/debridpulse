from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_downloads_final_helper.py <helper>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")


def replace_exact(old: str, new: str, expected: int = 1) -> None:
    global text
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"helper patch expected {expected} occurrence(s) of {old!r}, found {count}")
    text = text.replace(old, new)


# The helper is copied outside the worktree before detaching to the exact base.
# Resolve the repository from the workflow cwd, not from the temporary file path.
replace_exact(
    'ROOT = Path(__file__).resolve().parents[2]\n',
    'ROOT = Path.cwd().resolve()\n',
)

# Activity Refresh has both the older local icon rule and the later exact-parity
# rule. Migrate both occurrences in one guarded call, then remove the redundant
# later combined-selector migration that would otherwise see an already-updated
# source block.
replace_exact(
    'replace_exact(\n'
    '    controls,\n'
    '    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\\n",\n'
    '    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\\n"\n'
    '    "body.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\\n",\n'
    ')\n',
    'replace_exact(\n'
    '    controls,\n'
    '    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\\n",\n'
    '    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\\n"\n'
    '    "body.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\\n",\n'
    '    expected=2,\n'
    ')\n',
)
replace_exact(
    'replace_exact(\n'
    '    controls,\n'
    '    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\\n",\n'
    '    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\\n"\n'
    '    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\\n",\n'
    ')\n',
    'replace_exact(\n'
    '    controls,\n'
    '    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\\n",\n'
    '    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\\n"\n'
    '    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\\n",\n'
    '    expected=2,\n'
    ')\n',
)
replace_exact(
    'replace_exact(\n'
    '    controls,\n'
    '    "body.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\\n"\n'
    '    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\\n",\n'
    '    "body.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\\n"\n'
    '    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\\n"\n'
    '    "body.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\\n",\n'
    ')\n',
    '',
)
replace_exact(
    'replace_exact(\n'
    '    controls,\n'
    '    "body.light.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\\n"\n'
    '    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\\n",\n'
    '    "body.light.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\\n"\n'
    '    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\\n"\n'
    '    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\\n",\n'
    ')\n',
    '',
)

# A changed style-v11 import table needs a new outer style-v11 generation. The
# ui-runtime fallback embeds that URL, so it must advance too; operator-title is
# already being advanced because it embeds both presentation-runtime URLs.
replace_exact(
    'operator = STATIC / "operator-title.js"\n'
    'replace_exact(operator, "/ui-downloads-runtime.js?v=20", "/ui-downloads-runtime.js?v=21")\n'
    'index = STATIC / "index.html"\n'
    'replace_exact(index, "/operator-title.js?v=21", "/operator-title.js?v=22")\n',
    'operator = STATIC / "operator-title.js"\n'
    'replace_exact(operator, "/ui-downloads-runtime.js?v=20", "/ui-downloads-runtime.js?v=21")\n'
    'replace_exact(operator, "/ui-runtime.js?v=22", "/ui-runtime.js?v=23")\n'
    'presentation_runtime = STATIC / "ui-runtime.js"\n'
    'replace_exact(\n'
    '    presentation_runtime,\n'
    '    "if (!/style-v11\\\\.css\\\\?v=22$/.test(link.href)) link.href = \'/style-v11.css?v=22\';",\n'
    '    "if (!/style-v11\\\\.css\\\\?v=23$/.test(link.href)) link.href = \'/style-v11.css?v=23\';",\n'
    ')\n'
    'index = STATIC / "index.html"\n'
    'replace_exact(index, "/style-v11.css?v=22", "/style-v11.css?v=23")\n'
    'replace_exact(index, "/operator-title.js?v=21", "/operator-title.js?v=22")\n',
)

replace_exact(
    'version_replacements = (\n'
    '    ("/ui-dashboard-control-polish.css?v=22", "/ui-dashboard-control-polish.css?v=23"),\n',
    'version_replacements = (\n'
    '    ("/style-v11.css?v=22", "/style-v11.css?v=23"),\n'
    '    ("/ui-runtime.js?v=22", "/ui-runtime.js?v=23"),\n'
    '    ("/ui-dashboard-control-polish.css?v=22", "/ui-dashboard-control-polish.css?v=23"),\n',
)

# Explicit new-batch cache assertions need to include the outer chain as well.
replace_exact(
    '    assert "/ui-dashboard-control-polish.css?v=23" in style\\n'
    '    assert "/ui-downloads-page.css?v=26" in style\\n',
    '    assert "/style-v11.css?v=23" in index\\n'
    '    assert "/ui-runtime.js?v=23" in operator\\n'
    '    assert "/ui-dashboard-control-polish.css?v=23" in style\\n'
    '    assert "/ui-downloads-page.css?v=26" in style\\n',
)

# Replace a weak generated assertion with direct checks that the locally
# vendored action geometries exist in the Downloads presentation runtime.
replace_exact(
    '    for icon in ("pause", "play", "refresh", "trash", "x"):\\n'
    '        assert f"data-dp-bulk-icon=\\\\\\\"\' + icon" not in runtime  # dynamic marker remains generic\\n',
    '    for icon_name in ("pause:", "play:", "trash:", "x:"):\\n'
    '        assert icon_name in runtime\\n',
)

path.write_text(text, encoding="utf-8")
