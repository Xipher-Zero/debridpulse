from pathlib import Path


path = Path("backend/tests/test_direct_link_mirror_dedupe.py")
text = path.read_text()

old = '''    async def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("UPDATE download_files") and "status='duplicate'" in normalized:
            reason, file_id = params
            file_id = int(file_id)
            row = next((item for item in self.rows if item["file_id"] == file_id), None)
            if (
                row
                and row["status"] in {"pending", "queued", "paused"}
                and row.get("blocked") == 0
                and not row.get("download_id")
            ):
                row["status"] = "duplicate"
                row["blocked"] = None
                row["block_reason"] = str(reason)
                row["download_url"] = None
                row["local_path"] = None
                self.classified.append(file_id)
                return _Cursor(1)
            return _Cursor(0)
'''
new = '''    async def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if (
            normalized.startswith("UPDATE download_files")
            and "SET mirror_group_id=?" in normalized
            and "mirror_state=CASE" in normalized
        ):
            group_id, file_id = params
            file_id = int(file_id)
            row = next((item for item in self.rows if item["file_id"] == file_id), None)
            if not row:
                return _Cursor(0)
            row["mirror_group_id"] = int(group_id)
            if str(row.get("mirror_state") or "") in {"", "standby"}:
                row["mirror_state"] = "active"
            return _Cursor(1)
        if normalized.startswith("UPDATE download_files") and "status='duplicate'" in normalized:
            reason, group_id, file_id = params
            file_id = int(file_id)
            row = next((item for item in self.rows if item["file_id"] == file_id), None)
            if (
                row
                and row["status"] in {"pending", "queued", "paused"}
                and row.get("blocked") == 0
                and not row.get("download_id")
            ):
                row["status"] = "duplicate"
                row["blocked"] = None
                row["block_reason"] = str(reason)
                row["mirror_group_id"] = int(group_id)
                row["mirror_state"] = "standby"
                row["download_url"] = None
                row["local_path"] = None
                self.classified.append(file_id)
                return _Cursor(1)
            return _Cursor(0)
'''
if text.count(old) != 1:
    raise SystemExit(f"fake DB execute contract: expected one match, found {text.count(old)}")
text = text.replace(old, new, 1)

old_asserts = '''    assert db.rows[1]["status"] == "duplicate"
    assert db.rows[1]["blocked"] is None
    assert db.rows[1]["download_url"] is None
    assert db.rows[1]["local_path"] is None
    assert "Duplicate mirror of 1fichier.com" in db.rows[1]["block_reason"]
    assert db.rows[2]["status"] == "duplicate"
'''
new_asserts = '''    assert db.rows[0]["mirror_group_id"] == 1
    assert db.rows[0]["mirror_state"] == "active"
    assert db.rows[1]["status"] == "duplicate"
    assert db.rows[1]["blocked"] is None
    assert db.rows[1]["mirror_group_id"] == 1
    assert db.rows[1]["mirror_state"] == "standby"
    assert db.rows[1]["download_url"] is None
    assert db.rows[1]["local_path"] is None
    assert "Duplicate mirror of 1fichier.com" in db.rows[1]["block_reason"]
    assert db.rows[2]["status"] == "duplicate"
    assert db.rows[2]["mirror_group_id"] == 1
    assert db.rows[2]["mirror_state"] == "standby"
'''
if text.count(old_asserts) != 1:
    raise SystemExit(f"mirror assertions: expected one match, found {text.count(old_asserts)}")
text = text.replace(old_asserts, new_asserts, 1)
path.write_text(text)
print("Mirror failover test harness finalized")
