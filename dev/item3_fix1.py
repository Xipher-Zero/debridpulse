from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def replace_one(path, old, new):
    p = ROOT / path
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"Expected one match in {path}, found {text.count(old)}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1))


challenge_schema = '''\nINPUT_CHALLENGE_SCHEMA = (\n    """CREATE TABLE IF NOT EXISTS transfer_input_challenges (\n        transfer_id INTEGER PRIMARY KEY REFERENCES torrents(id),\n        challenge_id TEXT NOT NULL UNIQUE,\n        generation INTEGER NOT NULL CHECK(generation > 0),\n        reason TEXT NOT NULL,\n        origin TEXT NOT NULL,\n        integration_id TEXT NOT NULL,\n        operation_id TEXT NOT NULL,\n        request_id TEXT,\n        artifact_id INTEGER,\n        methods TEXT NOT NULL,\n        created_at REAL NOT NULL,\n        updated_at REAL NOT NULL\n    )""",\n    "CREATE INDEX IF NOT EXISTS idx_transfer_input_challenge_id ON transfer_input_challenges(challenge_id)",\n)\n\n_INPUT_CHALLENGE_COLUMNS = {\n    "transfer_id", "challenge_id", "generation", "reason", "origin", "integration_id",\n    "operation_id", "request_id", "artifact_id", "methods", "created_at", "updated_at",\n}\n'''

# Canonical DB initialization owns durable schema. The lifecycle store owns rows.
replace_one("backend/db/database.py", "\n_RUNTIME_STATE_COLUMNS = {", challenge_schema + "\n_RUNTIME_STATE_COLUMNS = {")
replace_one("backend/db/database.py", "        await db.execute(RUNTIME_STATE_SCHEMA[0])\n",
            "        await db.execute(RUNTIME_STATE_SCHEMA[0])\n        await db.execute(INPUT_CHALLENGE_SCHEMA[0])\n")
replace_one("backend/db/database.py", "            RUNTIME_STATE_SCHEMA[1],\n",
            "            RUNTIME_STATE_SCHEMA[1],\n            INPUT_CHALLENGE_SCHEMA[1],\n")
replace_one("backend/db/database.py", '            "integration_runtime_state": _RUNTIME_STATE_COLUMNS,\n',
            '            "integration_runtime_state": _RUNTIME_STATE_COLUMNS,\n            "transfer_input_challenges": _INPUT_CHALLENGE_COLUMNS,\n')

p = ROOT / "backend/transfers/input_required.py"
text = p.read_text()
text, n = re.subn(r'\n_SCHEMA = \(\n.*?\n\)\n\n_TERMINAL_FOR_INPUT', '\n_TERMINAL_FOR_INPUT', text, count=1, flags=re.S)
if n != 1:
    raise RuntimeError("Could not remove duplicate challenge schema owner")
old_init = '''    async def initialize(self):\n        async with get_db() as db:\n            for statement in _SCHEMA:\n                await db.execute(statement)\n            await db.commit()\n'''
new_init = '''    async def initialize(self):\n        # Canonical DB initialization owns schema creation. This store owns only\n        # challenge lifecycle rows.\n        return None\n'''
if text.count(old_init) != 1:
    raise RuntimeError("Could not canonicalize challenge store initialization")
p.write_text(text.replace(old_init, new_init, 1))

p = ROOT / "backend/tests/test_input_required_lifecycle.py"
text = p.read_text()
needle = "async def db_text():\n"
insert = '''@pytest.mark.asyncio\nasync def test_canonical_database_initialization_owns_challenge_schema(tmp_path, monkeypatch):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / "schema.sqlite3")\n    await database.init_db()\n    async with database.get_db() as db:\n        columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(transfer_input_challenges)")}\n    assert {"transfer_id", "challenge_id", "generation", "reason", "origin", "integration_id",\n            "operation_id", "request_id", "artifact_id", "methods", "created_at", "updated_at"} <= columns\n\n\n'''
if text.count(needle) != 1:
    raise RuntimeError("Could not insert schema ownership test")
p.write_text(text.replace(needle, insert + needle, 1))

print("Item 3 schema ownership correction applied")
