import ast
from pathlib import Path


BACKEND = Path(__file__).parents[1]
ROOT = BACKEND.parent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _function_source(path: Path, name: str) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in {path}")


def test_universal_transfer_and_application_policy_layers_import_no_concrete_integration_packages():
    paths = list((BACKEND / "transfers").glob("*.py")) + [
        BACKEND / "application" / "service.py",
        BACKEND / "application" / "observability.py",
        BACKEND / "core" / "scheduler.py",
    ]
    forbidden = ("providers.", "executors.", "postprocessors.")
    violations = []
    for path in paths:
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(BACKEND)} -> {imported}")
    assert violations == []


def test_providers_do_not_import_lifecycle_repository_scheduler_or_executors():
    forbidden = (
        "application.service", "transfers.engine", "transfers.repository",
        "core.scheduler", "executors.",
    )
    violations = []
    for path in (BACKEND / "providers").rglob("*.py"):
        for imported in _imports(path):
            if imported == forbidden[0] or imported == forbidden[1] or imported == forbidden[2] or imported == forbidden[3] or imported.startswith("executors."):
                violations.append(f"{path.relative_to(BACKEND)} -> {imported}")
    assert violations == []


def test_executors_do_not_import_provider_routing_or_lifecycle_owners():
    forbidden = ("providers.", "transfers.engine", "transfers.repository", "application.service")
    violations = []
    for path in (BACKEND / "executors").rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith("providers.") or imported in forbidden[1:]:
                violations.append(f"{path.relative_to(BACKEND)} -> {imported}")
    assert violations == []


def test_classifier_and_registry_are_concrete_integration_neutral():
    classifier = (BACKEND / "transfers" / "applicability.py").read_text().casefold()
    registry = (BACKEND / "transfers" / "registry.py").read_text().casefold()
    for token in ("alldebrid", "general_http", "general http", "aria2"):
        assert token not in classifier
        assert token not in registry
    assert "providers." not in classifier
    assert "integrations.runtime_state" not in classifier
    assert "specialized or generic" in classifier


def test_runtime_state_store_is_opaque_and_has_no_concrete_provider_interpreter():
    path = BACKEND / "integrations" / "runtime_state.py"
    source = path.read_text().casefold()
    imports = _imports(path)
    assert not any(name.startswith("providers.") for name in imports)
    for token in ("alldebrid", "general_http", "supported-hosts", "quota"):
        assert token not in source
    assert "payload" in source
    assert "schema_version" in source
    assert "generation" in source


def test_authentication_required_browser_runtime_has_no_provider_executor_or_url_routing_policy():
    source = (ROOT / "frontend" / "static" / "ui-auth-required.js").read_text().casefold()
    for token in ("alldebrid", "general_http", "aria2", "provider_id", "executor_id", "url.scheme", "url.protocol"):
        assert token not in source
    # OPENSSH markers are valid local key-format validation, not protocol routing.
    assert "username_password" in source
    assert "username_private_key" in source


def test_public_provenance_projection_uses_durable_provider_ids_not_url_classification():
    path = BACKEND / "api" / "routes.py"
    source = _function_source(path, "_public_transfer_presentation").casefold()
    assert "current_provider_id" in source
    assert "delivering_provider_id" in source
    assert "route_attempts" in source
    assert "execution_attempts" in source
    for token in ("urlparse", "urlsplit", "hostname", "alldebrid", "general_http"):
        assert token not in source


def test_concrete_integrations_are_registered_only_at_explicit_production_composition_boundaries():
    catalog = (BACKEND / "integrations" / "catalog.py").read_text()
    composition = (BACKEND / "application" / "composition.py").read_text()
    assert "providers.alldebrid.definition" in catalog
    assert "providers.general_http.definition" in catalog
    assert "executors.aria2.definition" in catalog
    assert "providers.alldebrid.host_runtime" in composition
    assert "executors.aria2.admin" in composition
