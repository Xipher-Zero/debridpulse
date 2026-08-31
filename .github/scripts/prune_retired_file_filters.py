from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def save(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def remove_between(text: str, start: str, end: str, label: str) -> str:
    start_count = text.count(start)
    end_count = text.count(end)
    if start_count != 1 or end_count < 1:
        raise SystemExit(
            f"{label}: expected one start and at least one end marker, "
            f"found start={start_count} end={end_count}"
        )
    a = text.index(start)
    b = text.index(end, a)
    return text[:a] + text[b:]


# ── Backend config model ──────────────────────────────────────────────────────
path = "backend/core/config.py"
text = load(path)
text = replace_once(
    text,
    '''    # Filters\n    filters_enabled: bool = False\n    blocked_extensions: List[str] = [\n        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",\n        ".svg", ".ico", ".tiff", ".heic", ".nfo", ".sfv"\n    ]\n    blocked_keywords: List[str] = []\n    min_file_size_mb: int = 0\n\n    # ── Smart File Selection ──────────────────────────────────────────────────\n    # Automatically block sample files, extras, and featurettes.\n    # Works alongside blocked_keywords — enabling this adds the most common\n    # sample/extra patterns without requiring manual keyword configuration.\n    block_samples: bool = False\n    block_extras: bool = False\n\n''',
    "",
    "remove automatic filter config fields",
)
text = replace_once(
    text,
    '''    # Labels / categories\n    torrent_labels: List[str] = []\n\n''',
    "",
    "remove unused predefined label config",
)
save(path, text)


# ── Backend config validator ──────────────────────────────────────────────────
path = "backend/core/config_validator.py"
text = load(path)
text = replace_once(
    text,
    '        "min_file_size_mb":               (0, 100_000),\n',
    "",
    "remove retired filter numeric validation",
)
text = replace_once(
    text,
    '''    for field in (\n        "blocked_extensions", "blocked_keywords", "torrent_labels",\n        "oidc_scopes", "oidc_allowed_subjects", "oidc_allowed_emails", "oidc_allowed_groups",\n    ):\n''',
    '''    for field in (\n        "oidc_scopes", "oidc_allowed_subjects", "oidc_allowed_emails", "oidc_allowed_groups",\n    ):\n''',
    "remove retired filter/list validation",
)
save(path, text)


# ── Backend automatic materialization policy ─────────────────────────────────
path = "backend/services/manager_v2.py"
text = load(path)
text = remove_between(
    text,
    "\ndef is_blocked(filename: str, cfg: AppSettings, size_bytes: int = 0) -> Tuple[bool, str]:\n",
    "\ndef fmt_bytes(size: int) -> str:\n",
    "remove automatic file filter evaluator",
)
text = replace_once(text, "        total_files = len(flat_files)\n", "", "remove filter-only total count")
text = replace_once(text, "        blocked_items: List[dict] = []\n", "", "remove filter-only blocked list")
text = replace_once(
    text,
    "        # Build work list: filter out duplicates and immediately-blocked files\n",
    "        # Build work list while collapsing duplicate provider entries.\n",
    "update manifest preparation comment",
)
text = replace_once(
    text,
    "            blocked, reason = is_blocked(display_name, cfg, file_size)\n",
    "",
    "remove automatic filter evaluation call",
)
text = replace_once(
    text,
    '''            if blocked:\n                blocked_items.append({"filename": display_name, "size_bytes": file_size, "reason": reason})\n                manifest_rows.append(\n                    (\n                        torrent_id,\n                        display_name,\n                        file_size,\n                        source_link,\n                        source_link,\n                        str(local_path),\n                        "blocked",\n                        client_name,\n                        1,\n                        reason,\n                    )\n                )\n                continue\n\n''',
    "",
    "remove automatic blocked manifest branch",
)
text = replace_once(
    text,
    '''        blocked_count = len(blocked_items)\n        failed_count = len(failed_items)\n        completed_count = len(transferred_items)\n        queued_count = len(queued_items)\n        downloadable_count = total_files - blocked_count\n\n''',
    '''        failed_count = len(failed_items)\n        completed_count = len(transferred_items)\n        queued_count = len(queued_items)\n\n''',
    "remove filter-only manifest counters",
)
text = replace_once(
    text,
    "        total_size_bytes = _size_sum(blocked_items + transferred_items + queued_items + failed_items)\n",
    "        total_size_bytes = _size_sum(transferred_items + queued_items + failed_items)\n",
    "remove filtered items from size accounting",
)
text = replace_once(
    text,
    '''        # All files go through aria2 — final_status is queued or error\n        if blocked_count == total_files and total_files > 0 and failed_count == 0:\n            # ALL files filtered — nothing to download; treat as completed so\n            # the torrent is removed from AllDebrid and counted in statistics.\n            final_status = "completed"\n        elif queued_count > 0:\n''',
    '''        # All provider files now enter the normal aria2 materialization path.\n        if queued_count > 0:\n''',
    "remove all-filtered completion branch",
)
text = replace_once(
    text,
    '''            # Build a descriptive event message\n            if blocked_count == total_files and total_files > 0:\n                _evt_msg = f"All {blocked_count} file(s) filtered/blocked — marked completed, removed from AllDebrid"\n                _evt_lvl = "info"\n            elif blocked_count > 0:\n                _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared, {blocked_count} filtered"\n                _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"\n            else:\n                _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared"\n                _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"\n''',
    '''            _evt_msg = f"Download {final_status}: {completed_count + queued_count} files prepared"\n            _evt_lvl = "info" if final_status in {"completed", "queued", "paused"} else "warn"\n''',
    "remove filter-specific event reporting",
)
text = replace_once(
    text,
    '''        await self._send_partial_summary(\n            torrent_id,\n            name,\n            flat_files,\n            blocked_items,\n            transferred_items + queued_items,\n            failed_items,\n        )\n\n''',
    "",
    "remove filter partial summary call",
)
text = replace_once(
    text,
    '''            # For all-blocked torrents: partial notification already sent above;\n            # skip the completed notification to avoid a confusing "0 files" message.\n            if cfg.discord_notify_finished and blocked_count < total_files:\n''',
    '''            if cfg.discord_notify_finished:\n''',
    "restore normal completion notification",
)
text = remove_between(
    text,
    "\n    async def _send_partial_summary(self, torrent_id: int, torrent_name: str, flat_files: List[Dict], blocked_items: List[dict], transferred_items: List[dict], failed_items: List[dict]):\n",
    "\n    # Direct download mode removed — aria2 handles all transfers\n",
    "remove automatic filter partial-summary helper",
)
save(path, text)


# ── Active Settings renderer/serializer ──────────────────────────────────────
path = "frontend/static/ui-settings-page.js"
text = load(path)
text = replace_once(
    text,
    '''    const filters = card('File Filters', `\n      ${toggle('filters_enabled', 'Enable File Filters', 'Apply extension, keyword, sample, extras, and size rules.', s.filters_enabled)}\n      <div class="dp-settings-filter-fields ${s.filters_enabled ? '' : 'is-disabled'}">\n        ${textarea('blocked_extensions', 'Blocked Extensions (one per line)', (s.blocked_extensions || []).join('\\n'), {rows: 5})}\n        ${textarea('blocked_keywords', 'Blocked Keywords (one per line)', (s.blocked_keywords || []).join('\\n'), {rows: 3})}\n        ${input('min_file_size_mb', 'Minimum File Size (MB)', s.min_file_size_mb ?? 0, {type: 'number', min: 0})}\n        ${toggle('block_samples', 'Block Samples / Trailers', 'Skip sample, trailer, and teaser files.', s.block_samples)}\n        ${toggle('block_extras', 'Block Extras / Featurettes', 'Skip common extras and featurette folders.', s.block_extras)}\n      </div>\n      ${input('torrent_labels_raw', 'Download Labels', (s.torrent_labels || []).join(', '), {\n        hint: 'Comma-separated labels available for downloads.'\n      })}\n    `);\n\n    return delivery + recovery + filters;\n''',
    '''    return delivery + recovery;\n''',
    "remove retired File Filters card",
)
text = replace_once(text, "    updateFilterState();\n", "", "remove retired filter render hook")
text = replace_once(
    text,
    '      if (event.target.matches(`[data-setting="filters_enabled"]`)) updateFilterState();\n',
    "",
    "remove retired filter change hook",
)
text = remove_between(
    text,
    "\n  function updateFilterState() {\n",
    "\n  function fieldFor(key) {\n",
    "remove retired filter state helper",
)
text = replace_once(
    text,
    '''      filters_enabled: boolOf('filters_enabled'),\n      blocked_extensions: linesOf('blocked_extensions'),\n      blocked_keywords: linesOf('blocked_keywords'),\n      min_file_size_mb: intOf('min_file_size_mb', 0),\n      block_samples: boolOf('block_samples'),\n      block_extras: boolOf('block_extras'),\n      torrent_labels: valueOf('torrent_labels_raw').split(',').map(item => item.trim()).filter(Boolean),\n\n''',
    "",
    "remove retired filter serializer fields",
)
if text.count("linesOf(") == 1:
    text = remove_between(
        text,
        "\n  function linesOf(key) {\n",
        "\n  function clearSecrets() {\n",
        "remove now-unused line-list helper",
    )
save(path, text)


# ── Settings completion presentation shim ────────────────────────────────────
path = "frontend/static/ui-settings-downloads-completion.js"
text = load(path)
text = replace_once(
    text,
    '''\n    /* UI-only retirement: keep the loaded controls intact so Apply Settings\n       preserves legacy values until their backend/config pruning pass. */\n    const fileFilters = cardByTitle(panel, 'File Filters');\n    if (fileFilters) {\n      fileFilters.classList.add('dp-settings-file-filters-retired');\n      fileFilters.setAttribute('aria-hidden', 'true');\n      fileFilters.inert = true;\n    }\n''',
    "",
    "remove obsolete File Filters hide shim",
)
save(path, text)

path = "frontend/static/ui-settings-downloads-completion.css"
text = load(path)
text = replace_once(
    text,
    '''\n/* File Filters is retired from the v1.0.11 Downloads presentation. */\nbody.dp-v11-structural #view-settings .dp-settings-file-filters-retired {\n  display: none !important;\n}\n''',
    "",
    "remove obsolete File Filters hide style",
)
save(path, text)


# ── Tests: retire obsolete automatic-filter expectations ─────────────────────
path = "backend/tests/test_download_logic.py"
text = load(path)
start = "    def test_is_blocked_respects_filters_enabled(self):\n"
end = "\n\n# ── all-blocked → completed ───────────────────────────────────────────────────\n"
text = remove_between(text, start, end, "remove is_blocked behavior tests")
start = "\n# ── all-blocked → completed ───────────────────────────────────────────────────\n\nclass TestAllBlockedStatus:\n"
end = "\n# ── _finalize_aria2_torrent logic ─────────────────────────────────────────────\n"
text = remove_between(text, start, end, "remove automatic all-filtered status tests")
text = text.replace("- all-blocked torrents: marked completed, not error\n", "")
text = text.replace("- partial-blocked torrents: continue with remaining files\n", "")
save(path, text)

path = "backend/tests/test_settings_downloads_completion_ui.py"
text = load(path)
start = "def test_file_filters_are_retired_from_presentation_without_destructive_ui_pass_rewrite():\n"
end = "\n\ndef test_safety_recovery_copy_uses_user_facing_titles_and_explanations():\n"
replacement = '''def test_file_filters_are_physically_retired_from_active_settings_runtime():\n    runtime = read("ui-settings-downloads-completion.js")\n    css = read("ui-settings-downloads-completion.css")\n    page = read("ui-settings-page.js")\n\n    assert "File Filters" not in page\n    for key in (\n        "filters_enabled",\n        "blocked_extensions",\n        "blocked_keywords",\n        "min_file_size_mb",\n        "block_samples",\n        "block_extras",\n        "torrent_labels_raw",\n    ):\n        assert key not in page\n\n    assert "dp-settings-file-filters-retired" not in runtime\n    assert ".dp-settings-file-filters-retired" not in css\n'''
a = text.index(start)
b = text.index(end, a)
text = text[:a] + replacement + text[b:]
save(path, text)

path = "backend/tests/test_settings_architecture_ui.py"
text = load(path)
old = '''        "aria2_error_retry_delay_seconds",\n        "filters_enabled",\n        "blocked_extensions",\n        "blocked_keywords",\n        "min_file_size_mb",\n        "block_samples",\n        "block_extras",\n        "torrent_labels_raw",\n        "aria2_split",\n'''
new = '''        "aria2_error_retry_delay_seconds",\n        "aria2_split",\n'''
text = replace_once(text, old, new, "remove retired settings inventory keys")
needle = '''    for key in (\n        "aria2_mode",\n'''
if needle not in text:
    raise SystemExit("settings architecture: downloads inventory anchor not found")
insert_anchor = '''    for key in (\n        "aria2_mode",\n'''
# Add explicit absence contract immediately before extraction inventory.
marker = '''    extraction = runtime[runtime.index("function extractionPanel"):runtime.index("function notificationsPanel")]\n'''
absence = '''    for retired in (\n        "filters_enabled",\n        "blocked_extensions",\n        "blocked_keywords",\n        "min_file_size_mb",\n        "block_samples",\n        "block_extras",\n        "torrent_labels_raw",\n    ):\n        assert retired not in downloads\n\n'''
text = replace_once(text, marker, absence + marker, "add retired settings absence contract")
save(path, text)

path = "backend/tests/test_settings_runtime_contract_census.py"
text = load(path)
addition = '''\n\ndef test_retired_file_filter_policy_is_physically_pruned_but_manual_blocking_and_labels_remain():\n    from core.config import AppSettings, _build_effective_settings\n\n    root = Path(__file__).resolve().parents[2]\n    retired = {\n        "filters_enabled",\n        "blocked_extensions",\n        "blocked_keywords",\n        "min_file_size_mb",\n        "block_samples",\n        "block_extras",\n        "torrent_labels",\n    }\n\n    assert retired.isdisjoint(AppSettings.model_fields)\n\n    legacy = {\n        "download_folder": "/download",\n        "filters_enabled": True,\n        "blocked_extensions": [".nfo"],\n        "blocked_keywords": ["sample"],\n        "min_file_size_mb": 100,\n        "block_samples": True,\n        "block_extras": True,\n        "torrent_labels": ["legacy"],\n    }\n    upgraded = _build_effective_settings(legacy)\n    assert upgraded.download_folder == "/download"\n    assert retired.isdisjoint(upgraded.model_dump())\n\n    manager = (root / "backend/services/manager_v2.py").read_text(encoding="utf-8")\n    assert "def is_blocked(" not in manager\n    assert "blocked_items" not in manager\n    assert "Filtered files were skipped" not in manager\n\n    routes = (root / "backend/api/routes.py").read_text(encoding="utf-8")\n    assert '@router.post("/torrents/{torrent_id}/files/{file_id}/block")' in routes\n    assert '@router.put("/torrents/{torrent_id}/label")' in routes\n    assert "SET status='blocked', blocked=1" in manager\n\n\ndef test_active_settings_runtime_contains_no_retired_file_filter_surface():\n    root = Path(__file__).resolve().parents[2]\n    page = (root / "frontend/static/ui-settings-page.js").read_text(encoding="utf-8")\n    completion = (root / "frontend/static/ui-settings-downloads-completion.js").read_text(encoding="utf-8")\n    completion_css = (root / "frontend/static/ui-settings-downloads-completion.css").read_text(encoding="utf-8")\n\n    for token in (\n        "File Filters",\n        "filters_enabled",\n        "blocked_extensions",\n        "blocked_keywords",\n        "min_file_size_mb",\n        "block_samples",\n        "block_extras",\n        "torrent_labels_raw",\n    ):\n        assert token not in page\n\n    assert "dp-settings-file-filters-retired" not in completion\n    assert "dp-settings-file-filters-retired" not in completion_css\n'''
if "test_retired_file_filter_policy_is_physically_pruned" in text:
    raise SystemExit("census retirement tests already exist")
text += addition
save(path, text)


# ── Final source-level guardrails on active owners ────────────────────────────
active_files = [
    "backend/core/config.py",
    "backend/core/config_validator.py",
    "backend/services/manager_v2.py",
    "frontend/static/ui-settings-page.js",
    "frontend/static/ui-settings-downloads-completion.js",
    "frontend/static/ui-settings-downloads-completion.css",
]
retired_tokens = (
    "filters_enabled",
    "blocked_extensions",
    "blocked_keywords",
    "min_file_size_mb",
    "block_samples",
    "block_extras",
    "torrent_labels",
)
for active in active_files:
    content = load(active)
    for token in retired_tokens:
        if token in content:
            raise SystemExit(f"retired token {token!r} remains in active owner {active}")

print("Retired automatic File Filters policy pruned from active owners.")
