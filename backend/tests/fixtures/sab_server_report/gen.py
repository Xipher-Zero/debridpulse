"""Run INSIDE the packaged image: SABnzbd 5.1.3's own report formatting.

Reproduces nzb/object.py set_download_report's "Servers" line with the real
misc.to_units, packs it as database.build_history_info does
("key:::actions;..."), and unpacks it with the real
database.unpack_history_info -- the exact stage_log the history API returns."""
import json, sys
sys.path.insert(0, "/app/usenet")
from sabnzbd.misc import to_units
from sabnzbd.database import unpack_history_info
from sabnzbd.version import __version__
import inspect, re
import sabnzbd.database as sabdb
_source = inspect.getsource(sabdb)
HISTORY_TABLE = re.search(r'(CREATE TABLE (?:IF NOT EXISTS )?"?history"? \(.*?\n\s*\))', _source, re.S).group(1)

def report(servers, counts):
    """servers: [(keyword, displayname, priority)]; counts: {keyword: bytes}"""
    ordered = sorted(servers, key=lambda s: "%02d%s" % (s[2], s[1].lower()))
    msgs = ["%s=%sB" % (name, to_units(counts[key])) for key, name, _ in ordered if key in counts]
    lines = ["Repair:::[x] Quick Check OK", "Servers:::" + ", ".join(msgs)] if msgs else ["Repair:::[x] Quick Check OK"]
    # A complete history row: every column of SABnzbd's own history table
    # (an in-memory copy of its schema), as the history API reads it.
    import sqlite3
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(HISTORY_TABLE)
    columns = [row[1] for row in db.execute("PRAGMA table_info(history)") if row[1] != "id"]
    values = {name: 0 for name in columns}
    values.update(name="job", script_log=b"", stage_log="\r\n".join(lines), status="Completed")
    db.execute("INSERT INTO history (%s) VALUES (%s)" % (",".join(columns), ",".join("?" * len(columns))),
               [values[name] for name in columns])
    return unpack_history_info(db.execute("SELECT * FROM history").fetchone())["stage_log"]

A, B, C = "dp-" + "a" * 32, "dp-" + "b" * 32, "dp-" + "c" * 32
cases = {
  "single": ([(A, "news.example.net", 0)], {A: 734003200}),
  "multiple": ([(A, "Primary", 0), (B, "Backup", 1)], {A: 1288490188, B: 5242880}),
  "primary_tried_nothing": ([(A, "Primary", 0), (B, "Fill", 1)], {B: 734003200}),
  "duplicate_names": ([(A, "news.example.net", 0), (B, "news.example.net", 0)], {A: 1024 * 1024 * 300, B: 2048}),
  "awkward_name": ([(A, "EU, west=1; fast", 0)], {A: 900}),
  "none": ([(A, "Primary", 0)], {}),
}
out = {"sabnzbd_version": __version__, "generated_by": "tests/fixtures/sab_server_report/gen.py (run in the packaged image)", "cases": {}}
for name, (servers, counts) in cases.items():
    out["cases"][name] = {"servers": [[k, d] for k, d, _ in servers], "counts": counts,
                          "stage_log": report(servers, counts)}
print(json.dumps(out, indent=1))
