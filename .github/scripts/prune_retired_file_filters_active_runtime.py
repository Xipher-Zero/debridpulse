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


path = "backend/services/transfer_integrity.py"
text = load(path)
text = replace_once(
    text,
    '''from services.manager_v2 import (\n    TorrentManager,\n    _size_sum,\n    is_blocked,\n    safe_name,\n    safe_rel_path,\n)\n''',
    '''from services.manager_v2 import (\n    TorrentManager,\n    _size_sum,\n    safe_name,\n    safe_rel_path,\n)\n''',
    "remove retired is_blocked import from active integrity owner",
)
text = replace_once(
    text,
    "        blocked_items: List[dict] = []\n",
    "",
    "remove active automatic blocked item accumulator",
)
text = replace_once(
    text,
    "            blocked, reason = is_blocked(display_name, cfg, file_size)\n",
    "",
    "remove active automatic filter evaluation",
)
text = replace_once(
    text,
    '''            if blocked:\n                blocked_items.append(\n                    {\n                        "filename": display_name,\n                        "size_bytes": file_size,\n                        "reason": reason,\n                    }\n                )\n                manifest_rows.append(\n                    (\n                        torrent_id,\n                        display_name,\n                        file_size,\n                        source_link,\n                        source_link,\n                        str(local_path),\n                        "blocked",\n                        client_name,\n                        1,\n                        reason,\n                    )\n                )\n                continue\n\n''',
    "",
    "remove active automatic blocked manifest branch",
)
text = replace_once(
    text,
    '''        blocked_count = len(blocked_items)\n        failed_count = len(failed_items)\n        completed_count = len(existing_items)\n        queued_count = len(queued_items)\n        manifest_count = len(manifest_rows)\n        accounted_count = (\n            blocked_count + failed_count + completed_count + queued_count\n        )\n        total_size_bytes = _size_sum(\n            blocked_items + existing_items + queued_items + failed_items\n        )\n''',
    '''        failed_count = len(failed_items)\n        completed_count = len(existing_items)\n        queued_count = len(queued_items)\n        manifest_count = len(manifest_rows)\n        accounted_count = failed_count + completed_count + queued_count\n        total_size_bytes = _size_sum(existing_items + queued_items + failed_items)\n''',
    "remove automatic-filter manifest accounting",
)
text = replace_once(
    text,
    '''        logger.info(\n            "integrity materializer: torrent %s provider=%d manifest=%d existing=%d "\n            "queued=%d blocked=%d failed=%d duplicates=%d",\n            torrent_id,\n            provider_file_count,\n            manifest_count,\n            completed_count,\n            queued_count,\n            blocked_count,\n            failed_count,\n            duplicate_entries,\n        )\n''',
    '''        logger.info(\n            "integrity materializer: torrent %s provider=%d manifest=%d existing=%d "\n            "queued=%d failed=%d duplicates=%d",\n            torrent_id,\n            provider_file_count,\n            manifest_count,\n            completed_count,\n            queued_count,\n            failed_count,\n            duplicate_entries,\n        )\n''',
    "remove filter count from active materializer logging",
)
text = replace_once(
    text,
    '''        elif blocked_count == manifest_count and failed_count == 0:\n            final_status = "completed"\n        elif queued_count > 0:\n            final_status = "queued"\n        elif failed_count > 0:\n            final_status = "error"\n        elif completed_count + blocked_count == manifest_count and completed_count > 0:\n            final_status = "completed"\n''',
    '''        elif queued_count > 0:\n            final_status = "queued"\n        elif failed_count > 0:\n            final_status = "error"\n        elif completed_count == manifest_count and completed_count > 0:\n            final_status = "completed"\n''',
    "remove all-filtered completion authority",
)
text = replace_once(
    text,
    '''            if blocked_count == manifest_count and manifest_count > 0:\n                event_message = (\n                    f"All {blocked_count} file(s) filtered/blocked — marked completed, "\n                    "removed from AllDebrid"\n                )\n                event_level = "info"\n            elif final_status == "completed" and completed_count > 0:\n''',
    '''            if final_status == "completed" and completed_count > 0:\n''',
    "remove active all-filtered event branch",
)
text = replace_once(
    text,
    '''                if blocked_count:\n                    details.append(f"{blocked_count} filtered")\n''',
    "",
    "remove filter event detail",
)
text = replace_once(
    text,
    '''\n        await self._send_partial_summary(\n            torrent_id,\n            name,\n            flat_files,\n            blocked_items,\n            existing_items + queued_items,\n            failed_items,\n        )\n''',
    "",
    "remove inherited retired filter summary call",
)
text = replace_once(
    text,
    '''            if (\n                cfg.discord_notify_finished\n                and blocked_count < manifest_count\n                and completed_count > 0\n            ):\n''',
    '''            if cfg.discord_notify_finished and completed_count > 0:\n''',
    "restore normal completion notification condition",
)
for token in (
    "is_blocked",
    "blocked_items",
    "blocked_count",
    "filtered/blocked",
    "_send_partial_summary",
):
    if token in text:
        raise SystemExit(f"retired automatic filter token remains in transfer_integrity.py: {token!r}")
save(path, text)


path = "backend/services/transfer_runtime_guard.py"
text = load(path)
text = replace_once(
    text,
    '''from services.manager_v2 import (\n    DIRECT_LINK_SOURCE,\n    READY_CODE,\n    extract_hash,\n    is_blocked,\n    safe_name,\n    safe_rel_path,\n)\n''',
    '''from services.manager_v2 import (\n    DIRECT_LINK_SOURCE,\n    READY_CODE,\n    extract_hash,\n    safe_name,\n    safe_rel_path,\n)\n''',
    "remove retired filter import from runtime guard",
)
text = replace_once(
    text,
    '''            file_size = int(file_info.get("size", 0) or 0)\n            blocked, _reason = is_blocked(display_name, cfg, file_size)\n            if blocked:\n                continue\n\n            relative_target = safe_rel_path(display_name)\n''',
    '''            file_size = int(file_info.get("size", 0) or 0)\n\n            relative_target = safe_rel_path(display_name)\n''',
    "make every provider file participate in path collision validation",
)
if "is_blocked" in text:
    raise SystemExit("retired is_blocked reference remains in transfer_runtime_guard.py")
save(path, text)


path = "backend/tests/test_settings_runtime_contract_census.py"
text = load(path)
old = '''    manager = (root / "backend/services/manager_v2.py").read_text(encoding="utf-8")\n    assert "def is_blocked(" not in manager\n    assert "blocked_items" not in manager\n    assert "Filtered files were skipped" not in manager\n\n    routes = (root / "backend/api/routes.py").read_text(encoding="utf-8")\n'''
new = '''    manager = (root / "backend/services/manager_v2.py").read_text(encoding="utf-8")\n    integrity = (root / "backend/services/transfer_integrity.py").read_text(encoding="utf-8")\n    runtime_guard = (root / "backend/services/transfer_runtime_guard.py").read_text(encoding="utf-8")\n    for owner in (manager, integrity, runtime_guard):\n        assert "is_blocked" not in owner\n    assert "blocked_items" not in manager\n    assert "blocked_items" not in integrity\n    assert "Filtered files were skipped" not in manager\n    assert "filtered/blocked" not in integrity\n    assert "_send_partial_summary" not in integrity\n\n    routes = (root / "backend/api/routes.py").read_text(encoding="utf-8")\n'''
text = replace_once(text, old, new, "strengthen active-owner retirement census")
save(path, text)

print("Retired File Filters policy pruned from active transfer runtime owners.")
