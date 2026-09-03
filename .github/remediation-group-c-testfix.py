"""Correct DB-001 test schema usage and static residue exposed by the gate."""
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    target.write_text(text.replace(old, new, 1))


path = Path("backend/tests/test_database_migration_ownership.py")
text = path.read_text()
old = '''        conn.execute("INSERT INTO integration_runtime_state(integration_id,state,detail) VALUES('provider-a','ready','stable')")\n'''
new = '''        conn.execute(\n            "INSERT INTO integration_runtime_state("\n            "integration_id,state_key,schema_version,payload,observed_at,stale_after,"\n            "successful_at,created_at,updated_at,generation"\n            ") VALUES(?,?,?,?,?,?,?,?,?,?)",\n            ("provider-a", "default", "1", b"stable", 1000.0, 2000.0, 1000.0, 900.0, 1000.0, 1),\n        )\n'''
if text.count(old) != 1:
    raise SystemExit(f"expected one fictitious runtime-state insert, found {text.count(old)}")
path.write_text(text.replace(old, new, 1))

replace_once("backend/main.py", "import asyncio\n", "")
replace_once(
    "backend/main.py",
    "from db.database import DatabaseMaintenanceActive, DB_PATH\n",
    "from db.database import DatabaseMaintenanceActive\n",
)
replace_once(
    "backend/transfers/engine.py",
    "    Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage,\n",
    "    Category, Domain, NormalizedError, Recovery, Retryability, Stage,\n",
)
