from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1))


# External 7z/RAR staging must be created on the writable destination filesystem.
replace_once(
    "backend/services/extraction_safety.py",
    '''        tempfile.mkdtemp(
            prefix=".debridpulse-extract-",
            dir=str(dest.parent),
        )''',
    '''        tempfile.mkdtemp(
            prefix=".debridpulse-extract-",
            dir=str(dest),
        )''',
    "external extraction staging location",
)

# Persist a human-readable extraction audit in the transfer event history and
# publish terminal errors to the browser so they can surface as a toast.
service = Path("backend/services/extraction_service.py")
text = service.read_text()
text = text.replace(
    '''    async def _publish_state(
        self,
        torrent_id: int,
        name: str,
        extraction_status: str,
    ) -> None:
''',
    '''    async def _publish_state(
        self,
        torrent_id: int,
        name: str,
        extraction_status: str,
        extraction_error: str | None = None,
    ) -> None:
''',
    1,
)
text = text.replace(
    '''                    "extraction_status": extraction_status,
''',
    '''                    "extraction_status": extraction_status,
                    "extraction_error": extraction_error,
''',
    1,
)
text = text.replace(
    '''        if not archives:
            return {"attempted": False, "reason": "no-archives"}
''',
    '''        if not archives:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET extraction_status='skipped', extraction_error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='completed'""",
                    (torrent_id,),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (torrent_id, "Auto-extract: Not attempted · no supported archive detected"),
                )
                await db.commit()
            return {"attempted": False, "reason": "no-archives"}
''',
    1,
)
text = text.replace(
    '''            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (torrent_id, f"Auto-extract started for {len(archives)} archive(s)"),
            )
''',
    '''            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (
                    torrent_id,
                    f"Auto-extract: Attempted · {len(archives)} archive(s) detected",
                ),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (torrent_id, "Extraction status: Extracting"),
            )
''',
    1,
)
text = text.replace(
    '''            event_message = (
                f"Auto-extract failed for {len(failures)} of {len(archives)} archive(s): {detail}"
            )[:1200]
''',
    '''            event_message = (
                f"Extraction status: Failed · {len(failures)}/{len(archives)} archive(s) failed · {detail}"
            )[:1200]
''',
    1,
)
text = text.replace(
    '''            event_message = f"Auto-extract completed for {len(successes)} archive(s)"
''',
    '''            event_message = (
                f"Extraction status: Completed · {len(successes)}/{len(archives)} archive(s) extracted"
            )
''',
    1,
)
text = text.replace(
    '''        await self._publish_state(torrent_id, name, final_state)
''',
    '''        await self._publish_state(
            torrent_id,
            name,
            final_state,
            detail or None,
        )
''',
    1,
)
service.write_text(text)

# Apply extraction lifecycle events directly so transient Extracting is never
# lost to a subsequent authoritative GET, and toast real terminal failures.
app = Path("frontend/static/app.js")
text = app.read_text()
anchor = '''function patchProgressOnlyTransferEvent(data) {
'''
helper = '''function patchExtractionTransferEvent(data) {
  const id = Number(data?.id ?? data?.torrent_id);
  const extractionStatus = String(data?.extraction_status || '').trim();

  if (!Number.isFinite(id) || !extractionStatus) {
    return false;
  }

  let displayStatus = 'completed';
  if (extractionStatus === 'extracting') {
    displayStatus = 'extracting';
  } else if (extractionStatus === 'error') {
    displayStatus = 'completed_with_errors';
  }

  document
    .querySelectorAll(`tr[data-torrent-id="${id}"]`)
    .forEach(row => {
      const statusCell = row.querySelector('[data-role="transfer-status"]');
      if (statusCell) statusCell.innerHTML = badge(displayStatus);
    });

  if (extractionStatus === 'error') {
    const reason = sanitizeErrorMsg(
      data?.extraction_error || 'Archive extraction failed'
    );
    toast(`Extraction failed: ${reason}`, 'error');
  }

  return true;
}

'''
if text.count(anchor) != 1:
    raise SystemExit("extraction SSE helper anchor missing or ambiguous")
text = text.replace(anchor, helper + anchor, 1)

old_handler = '''            const patchedProgress =
              patchProgressOnlyTransferEvent(payload);

            if (!patchedProgress) {
'''
new_handler = '''            const patchedExtraction =
              patchExtractionTransferEvent(payload);

            const patchedProgress =
              patchedExtraction
                ? false
                : patchProgressOnlyTransferEvent(payload);

            if (!patchedExtraction && !patchedProgress) {
'''
if text.count(old_handler) != 1:
    raise SystemExit("SSE handler anchor missing or ambiguous")
text = text.replace(old_handler, new_handler, 1)

old_tail = '''            } else if (!progressStatsTimer) {
              progressStatsTimer = setTimeout(
                ()=>{
                  progressStatsTimer = null;
                  loadStats().catch(()=>{});
                },
                1500
              );
            }
'''
new_tail = '''            } else if (!progressStatsTimer) {
              progressStatsTimer = setTimeout(
                ()=>{
                  progressStatsTimer = null;
                  loadStats().catch(()=>{});
                  if (patchedExtraction && payload.extraction_status !== 'extracting') {
                    if (document.getElementById('view-torrents')?.classList.contains('active')) {
                      loadTorrents().catch(()=>{});
                    }
                    if (document.getElementById('view-dashboard')?.classList.contains('active')) {
                      loadRecent().catch(()=>{});
                    }
                  }
                },
                1500
              );
            }
'''
if text.count(old_tail) != 1:
    raise SystemExit("SSE refresh tail anchor missing or ambiguous")
text = text.replace(old_tail, new_tail, 1)
app.write_text(text)

# Cache-bust the modified app bundle and advance contract tests.
replace_once(
    "frontend/static/index.html",
    '<script src="/app.js?v=13" defer></script>',
    '<script src="/app.js?v=14" defer></script>',
    "app cache bust",
)
for test_path in (
    "backend/tests/test_dashboard_startup_surface.py",
    "backend/tests/test_operator_title_state.py",
    "backend/tests/test_v1_scope.py",
):
    replace_once(test_path, "/app.js?v=13", "/app.js?v=14", f"{test_path} cache contract")

# Regression coverage for writable staging and durable operator events.
test = Path("backend/tests/test_extraction_lifecycle.py")
text = test.read_text()
text += '''\n\n@pytest.mark.asyncio\nasync def test_extraction_events_form_durable_operator_audit(tmp_path, monkeypatch):\n    db_path = await _prepare_db(tmp_path, monkeypatch)\n    archive = tmp_path / "payload.zip"\n    with zipfile.ZipFile(archive, "w") as zf:\n        zf.writestr("payload.txt", b"payload")\n    torrent_id = _insert_completed(db_path, archive)\n    monkeypatch.setattr(extraction_service, "get_settings", lambda: _settings())\n    monkeypatch.setattr(extraction_service, "publish", AsyncMock())\n\n    await ExtractionService().extract_completed_transfer(torrent_id)\n\n    conn = sqlite3.connect(db_path)\n    try:\n        events = [\n            row[0]\n            for row in conn.execute(\n                "SELECT message FROM events WHERE torrent_id=? ORDER BY id",\n                (torrent_id,),\n            ).fetchall()\n        ]\n    finally:\n        conn.close()\n\n    assert "Auto-extract: Attempted · 1 archive(s) detected" in events\n    assert "Extraction status: Extracting" in events\n    assert "Extraction status: Completed · 1/1 archive(s) extracted" in events\n\n\ndef test_external_extraction_stages_inside_destination(tmp_path, monkeypatch):\n    from services.extraction_safety import staged_external_extract\n\n    archive = tmp_path / "payload.rar"\n    archive.write_bytes(b"archive")\n    dest = tmp_path / "download"\n    dest.mkdir()\n    observed = {}\n\n    def runner(stage):\n        observed["stage"] = Path(stage)\n        (Path(stage) / "payload.txt").write_text("ok")\n\n    monkeypatch.setattr(\n        "services.extraction_safety.validate_extracted_tree",\n        lambda stage, archive: None,\n    )\n    staged_external_extract(archive, dest, runner)\n\n    assert observed["stage"].parent == dest.resolve()\n    assert (dest / "payload.txt").read_text() == "ok"\n\n\ndef test_frontend_surfaces_extraction_failure_toast():\n    root = Path(__file__).resolve().parents[2]\n    app_source = (root / "frontend/static/app.js").read_text()\n    assert "patchExtractionTransferEvent" in app_source\n    assert "Extraction failed:" in app_source\n    assert "extraction_error" in app_source\n'''
test.write_text(text)

print("Extraction observability correction applied")
