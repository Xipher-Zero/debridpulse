from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "backend/tests"


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    source = path.read_text()
    if old not in source:
        raise RuntimeError(f"{label} changed")
    path.write_text(source.replace(old, new, 1))


replace_once(
    TESTS / "test_provider_applicability.py",
    '    monkeypatch.setattr(database, "DB_PATH", db_path)\n    store = ProviderRuntimeStateStore()\n',
    '    monkeypatch.setattr(database, "DB_PATH", db_path)\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n',
    "provider applicability runtime-state fixture",
)

pattern = TESTS / "test_alldebrid_pattern_applicability.py"
source = pattern.read_text()
old = '    monkeypatch.setattr(database, "DB_PATH", tmp_path / "patterns.sqlite3")\n    store = ProviderRuntimeStateStore()\n'
new = '    monkeypatch.setattr(database, "DB_PATH", tmp_path / "patterns.sqlite3")\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n'
if old not in source:
    raise RuntimeError("AllDebrid regexp fixture changed")
source = source.replace(old, new, 1)
old = '    monkeypatch.setattr(database, "DB_PATH", tmp_path / "patterns-static.sqlite3")\n    store = ProviderRuntimeStateStore()\n'
new = '    monkeypatch.setattr(database, "DB_PATH", tmp_path / "patterns-static.sqlite3")\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n'
if old not in source:
    raise RuntimeError("AllDebrid static capability fixture changed")
source = source.replace(old, new, 1)
pattern.write_text(source)

replace_once(
    TESTS / "test_alldebrid_host_runtime.py",
    'async def runtime_store(tmp_path, monkeypatch, name):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)\n    store = ProviderRuntimeStateStore()\n',
    'async def runtime_store(tmp_path, monkeypatch, name):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n',
    "AllDebrid host runtime fixture",
)

replace_once(
    TESTS / "test_alldebrid_host_runtime_acceptance.py",
    'async def runtime_store(tmp_path: Path, monkeypatch, name: str):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)\n    store = ProviderRuntimeStateStore()\n',
    'async def runtime_store(tmp_path: Path, monkeypatch, name: str):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)\n    await database.init_db()\n    store = ProviderRuntimeStateStore()\n',
    "AllDebrid host acceptance fixture",
)

replace_once(
    TESTS / "test_route_provider_provenance.py",
    '    migrated = TransferRepository()\n    await migrated.initialize()\n    # DB-001: ordinary repository startup owns current-schema readiness only.\n',
    '    # Re-enter through the canonical database owner to recreate current-schema\n    # tables before ordinary repository startup validates them.\n    await database.init_db()\n    migrated = TransferRepository()\n    await migrated.initialize()\n    # DB-001: ordinary repository startup validates current-schema readiness only.\n',
    "provenance migration fixture",
)

replace_once(
    TESTS / "test_universal_boundaries.py",
    '            if path.name in {"engine.py", "policy.py", "registry.py"} and isinstance(node, ast.Attribute):\n                assert node.attr not in {"native_code", "diagnostic"}, (path, node.attr)\n',
    '            if path.name in {"engine.py", "policy.py", "registry.py"} and isinstance(node, ast.Attribute):\n                # Native provider codes remain forbidden everywhere in universal policy.\n                # A diagnostic value is provider-neutral data: engine cleanup may preserve a\n                # sanitized diagnostic, while retry/selection policy must never branch on it.\n                forbidden = {"native_code"}\n                if path.name in {"policy.py", "registry.py"}:\n                    forbidden.add("diagnostic")\n                assert node.attr not in forbidden, (path, node.attr)\n',
    "universal diagnostic boundary",
)
