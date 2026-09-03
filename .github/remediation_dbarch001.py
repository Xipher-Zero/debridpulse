from __future__ import annotations

import ast
from pathlib import Path
from pprint import pformat

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT / "backend/transfers/repository.py"
DATABASE = ROOT / "backend/db/database.py"
RUNTIME_STATE = ROOT / "backend/integrations/runtime_state.py"
TEST = ROOT / "backend/tests/test_second_pass_dbarch001.py"


def assigned_literal(source: str, name: str):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value), node.lineno, node.end_lineno
    raise RuntimeError(f"missing assignment {name}")


def remove_line_ranges(source: str, ranges: list[tuple[int, int]]) -> str:
    lines = source.splitlines(keepends=True)
    for start, end in sorted(ranges, reverse=True):
        del lines[start - 1:end]
    return "".join(lines)


def replace_exact(path: Path, old: str, new: str, *, label: str) -> None:
    source = path.read_text()
    if old not in source:
        raise RuntimeError(f"{label} changed")
    path.write_text(source.replace(old, new, 1))


repo_source = REPOSITORY.read_text()
schema, schema_start, schema_end = assigned_literal(repo_source, "_SCHEMA")
columns, columns_start, columns_end = assigned_literal(repo_source, "_COLUMNS")
repo_source = remove_line_ranges(repo_source, [(schema_start, schema_end), (columns_start, columns_end)])
repo_source = repo_source.replace(
    "from db.database import get_db\n",
    "from db.database import get_db, validate_transfer_repository_schema\n",
    1,
)
old_initialize = '''    async def initialize(self) -> None:\n        async with get_db() as db:\n            await db.execute("BEGIN IMMEDIATE")\n            for statement in _SCHEMA:\n                await db.execute(statement)\n            for table, definitions in _COLUMNS.items():\n                existing = {row["name"] for row in await db.fetchall(f"PRAGMA table_info({table})")}\n                for column, definition in definitions.items():\n                    if column not in existing:\n                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")\n            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")\n            await db.commit()\n'''
new_initialize = '''    async def initialize(self) -> None:\n        # Runtime repositories consume the schema; database bootstrap/migration\n        # is the sole authority allowed to create or repair it.\n        await validate_transfer_repository_schema()\n'''
if old_initialize not in repo_source:
    raise RuntimeError("repository initialize owner changed")
repo_source = repo_source.replace(old_initialize, new_initialize, 1)
REPOSITORY.write_text(repo_source)

required_columns = {
    "application_events": {"id", "transfer_id", "kind", "detail", "claimed", "created_at"},
    "postprocess_attempts": {"transfer_id", "processor_id", "state", "paths", "outcome"},
    "transfer_controls": {"key", "value"},
    "transfer_requests": {"id", "transfer_id", "parent_id", "ordinal", "payload", "state", "resource", "attempts", "retry_at", "error", "metadata"},
    "provider_resources": {"id", "transfer_id", "provider_id", "payload", "state", "cleanup_authority", "cleanup_error", "updated_at", "cleanup_attempts", "cleanup_retry_at", "cleanup_blocked"},
    "resolution_attempts": {"id", "request_id", "provider_id", "state", "error", "created_at", "updated_at", "result"},
    "execution_attempts": {"id", "transfer_id", "artifact_id", "executor_id", "handle", "state", "authorized", "progress", "error", "created_at", "updated_at", "candidate", "progress_at", "cleanup_state", "cleanup_attempts", "cleanup_retry_at", "cleanup_error"},
    "route_attempt_provenance": {"resolution_attempt_id", "transfer_id", "request_id", "ordinal", "operation", "previous_attempt_id", "transition_kind", "transition_reason", "candidate_summary", "outcome", "history_quality", "created_at", "updated_at"},
    "execution_attempt_provenance": {"execution_attempt_id", "route_attempt_id", "transfer_id", "artifact_id", "ordinal", "provider_id", "candidate_id", "candidate_source", "outcome", "delivered", "history_quality", "created_at", "updated_at"},
    "transfer_outcomes": {"id", "transfer_id", "attempt_id", "kind", "payload", "created_at"},
    "torrents": set(columns["torrents"]),
    "download_files": set(columns["download_files"]),
}

db_source = DATABASE.read_text()
anchor = '''_RUNTIME_STATE_COLUMNS = {\n    "integration_id",\n    "state_key",\n    "schema_version",\n    "payload",\n    "observed_at",\n    "stale_after",\n    "successful_at",\n    "created_at",\n    "updated_at",\n    "generation",\n}\n\n\n'''
if anchor not in db_source:
    raise RuntimeError("database schema anchor changed")
insert = (
    "TRANSFER_REPOSITORY_SCHEMA = " + pformat(schema, width=110, sort_dicts=False) + "\n\n"
    + "TRANSFER_REPOSITORY_COLUMNS = " + pformat(columns, width=110, sort_dicts=False) + "\n\n"
    + "_TRANSFER_REPOSITORY_REQUIRED_COLUMNS = " + pformat(required_columns, width=110, sort_dicts=True) + "\n\n\n"
    + '''async def _validate_schema_readonly(required: dict[str, set[str]], *, owner: str) -> None:\n    path = Path(DB_PATH)\n    if not path.exists() or path.stat().st_size == 0:\n        raise RuntimeError(f"{owner} schema is unavailable; database bootstrap must run first")\n    uri = path.resolve().as_uri() + "?mode=ro"\n    try:\n        async with aiosqlite.connect(uri, uri=True) as db:\n            check = await (await db.execute("PRAGMA quick_check")).fetchone()\n            if not check or check[0] != "ok":\n                raise RuntimeError(f"{owner} schema failed SQLite integrity verification")\n            missing_by_table: dict[str, list[str]] = {}\n            for table, expected in required.items():\n                rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()\n                present = {row[1] for row in rows}\n                missing = sorted(expected - present)\n                if not rows or missing:\n                    missing_by_table[table] = missing or ["<table>"]\n            if missing_by_table:\n                raise RuntimeError(f"{owner} schema is incomplete: {missing_by_table}")\n    except aiosqlite.Error as exc:\n        raise RuntimeError(f"{owner} schema could not be verified read-only") from exc\n\n\nasync def validate_transfer_repository_schema() -> None:\n    await _validate_schema_readonly(_TRANSFER_REPOSITORY_REQUIRED_COLUMNS, owner="transfer repository")\n\n\nasync def validate_runtime_state_schema() -> None:\n    await _validate_schema_readonly({"integration_runtime_state": _RUNTIME_STATE_COLUMNS}, owner="integration runtime state")\n\n\n'''
)
db_source = db_source.replace(anchor, anchor + insert, 1)

old_create = '''        await db.execute(RUNTIME_STATE_SCHEMA[0])\n        await db.execute(INPUT_CHALLENGE_SCHEMA[0])\n        for col, defn in _SCHEMA_COLUMNS_TORRENTS:\n            await _ensure_column(db, "torrents", col, defn)\n        for col, defn in _SCHEMA_COLUMNS_FILES:\n            await _ensure_column(db, "download_files", col, defn)\n        await db.commit()\n'''
new_create = '''        await db.execute(RUNTIME_STATE_SCHEMA[0])\n        await db.execute(INPUT_CHALLENGE_SCHEMA[0])\n        for statement in TRANSFER_REPOSITORY_SCHEMA:\n            await db.execute(statement)\n        for col, defn in _SCHEMA_COLUMNS_TORRENTS:\n            await _ensure_column(db, "torrents", col, defn)\n        for col, defn in _SCHEMA_COLUMNS_FILES:\n            await _ensure_column(db, "download_files", col, defn)\n        for table, definitions in TRANSFER_REPOSITORY_COLUMNS.items():\n            for column, definition in definitions.items():\n                await _ensure_column(db, table, column, definition)\n        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")\n        await db.commit()\n'''
if old_create not in db_source:
    raise RuntimeError("database creation owner changed")
db_source = db_source.replace(old_create, new_create, 1)

old_required = '''            "integration_runtime_state": _RUNTIME_STATE_COLUMNS,\n            "transfer_input_challenges": _INPUT_CHALLENGE_COLUMNS,\n        }\n'''
new_required = '''            "integration_runtime_state": _RUNTIME_STATE_COLUMNS,\n            "transfer_input_challenges": _INPUT_CHALLENGE_COLUMNS,\n        }\n        for table, expected in _TRANSFER_REPOSITORY_REQUIRED_COLUMNS.items():\n            required.setdefault(table, set()).update(expected)\n'''
if old_required not in db_source:
    raise RuntimeError("database verification owner changed")
db_source = db_source.replace(old_required, new_required, 1)
DATABASE.write_text(db_source)

runtime_source = RUNTIME_STATE.read_text()
runtime_source = runtime_source.replace(
    "from db.database import RUNTIME_STATE_SCHEMA, get_db\n",
    "from db.database import get_db, validate_runtime_state_schema\n",
    1,
)
schema_method = '''    @staticmethod\n    def _schema_statements() -> tuple[str, ...]:\n        return RUNTIME_STATE_SCHEMA\n\n'''
if schema_method not in runtime_source:
    raise RuntimeError("runtime-state schema method changed")
runtime_source = runtime_source.replace(schema_method, "", 1)
old_runtime_initialize = '''    async def initialize(self) -> None:\n        """Create the neutral schema transactionally and idempotently."""\n        if self._initialized:\n            return\n        async with self._initialize_lock:\n            if self._initialized:\n                return\n            try:\n                async with get_db() as db:\n                    await db.execute("BEGIN IMMEDIATE")\n                    try:\n                        for statement in self._schema_statements():\n                            await db.execute(statement)\n                        await db.commit()\n                    except Exception:\n                        await db.rollback()\n                        raise\n            except Exception as exc:\n                raise RuntimeStateStorageError("Could not initialize integration runtime-state persistence") from exc\n            self._initialized = True\n'''
new_runtime_initialize = '''    async def initialize(self) -> None:\n        """Verify bootstrap-owned schema without creating or repairing it."""\n        if self._initialized:\n            return\n        async with self._initialize_lock:\n            if self._initialized:\n                return\n            try:\n                await validate_runtime_state_schema()\n            except Exception as exc:\n                raise RuntimeStateStorageError("Could not initialize integration runtime-state persistence") from exc\n            self._initialized = True\n'''
if old_runtime_initialize not in runtime_source:
    raise RuntimeError("runtime-state initialize owner changed")
runtime_source = runtime_source.replace(old_runtime_initialize, new_runtime_initialize, 1)
RUNTIME_STATE.write_text(runtime_source)

TEST.write_text('''from __future__ import annotations\n\nimport sqlite3\n\nimport pytest\nfrom pathlib import Path\n\nimport db.database as database\nfrom integrations.runtime_state import ProviderRuntimeStateStore, RuntimeStateStorageError\nfrom transfers.repository import TransferRepository\n\n\ndef _use_database(monkeypatch, tmp_path):\n    path = tmp_path / "dbarch.sqlite3"\n    monkeypatch.setattr(database, "DB_PATH", path)\n    return path\n\n\n@pytest.mark.asyncio\nasync def test_repository_runtime_initializer_cannot_create_absent_database(monkeypatch, tmp_path):\n    path = _use_database(monkeypatch, tmp_path)\n    with pytest.raises(RuntimeError, match="bootstrap must run first"):\n        await TransferRepository().initialize()\n    assert not path.exists()\n\n\n@pytest.mark.asyncio\nasync def test_provider_runtime_state_initializer_cannot_create_absent_database(monkeypatch, tmp_path):\n    path = _use_database(monkeypatch, tmp_path)\n    with pytest.raises(RuntimeStateStorageError):\n        await ProviderRuntimeStateStore().initialize()\n    assert not path.exists()\n\n\n@pytest.mark.asyncio\nasync def test_database_bootstrap_owns_schema_then_runtime_initializers_only_validate(monkeypatch, tmp_path):\n    path = _use_database(monkeypatch, tmp_path)\n    await database.init_db()\n    assert path.exists()\n    await TransferRepository().initialize()\n    await ProviderRuntimeStateStore().initialize()\n\n\n@pytest.mark.asyncio\nasync def test_repository_initializer_rejects_missing_canonical_table_without_repair(monkeypatch, tmp_path):\n    path = _use_database(monkeypatch, tmp_path)\n    await database.init_db()\n    with sqlite3.connect(path) as conn:\n        conn.execute("DROP TABLE transfer_outcomes")\n        conn.commit()\n    with pytest.raises(RuntimeError, match="transfer repository schema is incomplete"):\n        await TransferRepository().initialize()\n    with sqlite3.connect(path) as conn:\n        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_outcomes'").fetchone()\n    assert present is None\n\n\n@pytest.mark.asyncio\nasync def test_runtime_state_initializer_rejects_missing_table_without_repair(monkeypatch, tmp_path):\n    path = _use_database(monkeypatch, tmp_path)\n    await database.init_db()\n    with sqlite3.connect(path) as conn:\n        conn.execute("DROP TABLE integration_runtime_state")\n        conn.commit()\n    with pytest.raises(RuntimeStateStorageError):\n        await ProviderRuntimeStateStore().initialize()\n    with sqlite3.connect(path) as conn:\n        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'").fetchone()\n    assert present is None\n\n\ndef test_runtime_components_contain_no_schema_mutation_authority():\n    repository_source = (Path(__file__).parents[1] / "transfers/repository.py").read_text()\n    runtime_source = (Path(__file__).parents[1] / "integrations/runtime_state.py").read_text()\n    for source in (repository_source, runtime_source):\n        upper = source.upper()\n        assert "CREATE TABLE" not in upper\n        assert "ALTER TABLE" not in upper\n        assert "CREATE INDEX" not in upper\n''')

# Runtime-state semantic tests now enter through the canonical database bootstrap
# rather than relying on the runtime store to create its own persistence schema.
state_tests = ROOT / "backend/tests/test_provider_runtime_state.py"
state_source = state_tests.read_text()
state_source = state_source.replace("import pytest\n", "import pytest\nimport pytest_asyncio\n", 1)
old_fixture = '''@pytest.fixture\ndef isolated_database(tmp_path, monkeypatch):\n    path = tmp_path / "runtime-state.sqlite3"\n    monkeypatch.setattr(database, "DB_PATH", path)\n    return path\n'''
new_fixture = '''@pytest_asyncio.fixture\nasync def isolated_database(tmp_path, monkeypatch):\n    path = tmp_path / "runtime-state.sqlite3"\n    monkeypatch.setattr(database, "DB_PATH", path)\n    await database.init_db()\n    return path\n'''
if old_fixture not in state_source:
    raise RuntimeError("runtime-state isolated database fixture changed")
state_source = state_source.replace(old_fixture, new_fixture, 1)
start = state_source.index("@pytest.mark.asyncio\nasync def test_schema_failure_rolls_back_without_false_partial_success")
end = state_source.index("\n\n@pytest.mark.asyncio\nasync def test_database_open_failure_is_reported_as_bounded_storage_error", start)
replacement = '''@pytest.mark.asyncio\nasync def test_runtime_initializer_rejects_missing_schema_without_repair(isolated_database):\n    async with aiosqlite.connect(isolated_database) as db:\n        await db.execute("DROP TABLE integration_runtime_state")\n        await db.commit()\n\n    with pytest.raises(RuntimeStateStorageError):\n        await ProviderRuntimeStateStore().initialize()\n\n    async with aiosqlite.connect(isolated_database) as db:\n        present = await (await db.execute(\n            "SELECT name FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'"\n        )).fetchone()\n    assert present is None\n'''
state_source = state_source[:start] + replacement + state_source[end:]
state_tests.write_text(state_source)

metadata_tests = ROOT / "backend/tests/test_provider_runtime_state_metadata.py"
replace_exact(
    metadata_tests,
    '    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"non-finite-{field}.sqlite3")\n    store = ProviderRuntimeStateStore()\n',
    '    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"non-finite-{field}.sqlite3")\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n',
    label="runtime-state metadata bootstrap",
)

isolation_tests = ROOT / "backend/tests/test_provider_runtime_state_provider_isolation.py"
replace_exact(
    isolation_tests,
    '    monkeypatch.setattr(database, "DB_PATH", tmp_path / "provider-isolation.sqlite3")\n    store = ProviderRuntimeStateStore()\n',
    '    monkeypatch.setattr(database, "DB_PATH", tmp_path / "provider-isolation.sqlite3")\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n',
    label="provider-isolation bootstrap",
)

transaction_tests = ROOT / "backend/tests/test_provider_runtime_state_transactions.py"
transaction_source = transaction_tests.read_text()
old_replacement_setup = '''    db_path = tmp_path / "replacement-rollback.sqlite3"\n    monkeypatch.setattr(database, "DB_PATH", db_path)\n    store = ProviderRuntimeStateStore()\n'''
new_replacement_setup = '''    db_path = tmp_path / "replacement-rollback.sqlite3"\n    monkeypatch.setattr(database, "DB_PATH", db_path)\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n'''
if old_replacement_setup not in transaction_source:
    raise RuntimeError("runtime-state replacement transaction setup changed")
transaction_source = transaction_source.replace(old_replacement_setup, new_replacement_setup, 1)
old_marker_call = '''        await db.commit()\n\n    await ProviderRuntimeStateStore().initialize()\n\n    async with aiosqlite.connect(db_path) as db:\n'''
new_marker_call = '''        await db.commit()\n\n    await database.init_db()\n    await ProviderRuntimeStateStore().initialize()\n\n    async with aiosqlite.connect(db_path) as db:\n'''
if old_marker_call not in transaction_source:
    raise RuntimeError("migration marker runtime-state setup changed")
transaction_source = transaction_source.replace(old_marker_call, new_marker_call, 1)
transaction_tests.write_text(transaction_source)
