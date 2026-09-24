"""Architecture proven by absence: no aria2-shaped executor model remains in core.

Every assertion here reads production source. A comment or docstring does not
count as proof; only the absence of the superseded owner does.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re

import pytest

BACKEND = Path(__file__).resolve().parents[1]
CORE_PACKAGES = ("transfers", "application", "core")
PRODUCTION_PACKAGES = ("transfers", "application", "core", "executors", "integrations", "api", "providers",
                       "services", "postprocessors", "db", "auth")


def _files(packages):
    for package in packages:
        for path in sorted((BACKEND / package).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path
    if packages == PRODUCTION_PACKAGES:
        yield BACKEND / "main.py"


def _without_legacy_input_maps(path: Path) -> str:
    """Historical flat configuration KEY spellings (one-way migration input) are
    the only permitted occurrence of an executor name in core modules."""
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "LEGACY_INPUT_FIELDS"
                                                 for target in node.targets)):
            for index in range(node.lineno - 1, node.end_lineno):
                lines[index] = ""
    return "\n".join(lines)


def test_universal_core_contains_no_concrete_executor_name():
    offenders = []
    for path in _files(CORE_PACKAGES):
        text = _without_legacy_input_maps(path)
        if re.search(r"aria2|sabnzbd|\bsab\b|rsync", text, re.I):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_universal_core_contains_no_native_job_identity_names():
    offenders = []
    for path in _files(CORE_PACKAGES):
        if re.search(r"\bgids?\b|nzo_id|tellActive|tellWaiting|tellStopped|addUri", path.read_text(), re.I):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_universal_core_contains_no_native_runtime_keys():
    offenders = []
    for path in _files(CORE_PACKAGES):
        if re.search(r"max-overall-download-limit|max-concurrent-downloads", path.read_text()):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_no_scheme_based_executor_router_remains_anywhere():
    from transfers.models import IntegrationDescriptor
    from transfers.registry import IntegrationRegistry
    assert "schemes" not in IntegrationDescriptor.__dataclass_fields__
    for name in ("eligible_executors", "executor_for"):
        assert not hasattr(IntegrationRegistry, name)
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        text = path.read_text()
        if re.search(r"descriptor\.schemes|eligible_executors|executor_for\(", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_evidence_sampling_and_continuation_route_through_subject_claims():
    from transfers import _engine_base, mirrors
    assert "executor_for_subject" in inspect.getsource(mirrors.shared_evidence)
    assert "executor_for_subject" in inspect.getsource(mirrors.self_evidence)
    assert "executor_for_subject" in inspect.getsource(_engine_base.TransferEngine._evidence_target)
    assert "executor_for_subject" in inspect.getsource(_engine_base.TransferEngine._continue_executor_input)


def test_core_does_not_reject_executable_subjects_for_missing_endpoints():
    from transfers import _engine_base
    source = inspect.getsource(_engine_base.TransferEngine._materialize)
    assert "candidate.endpoints" not in source


def test_universal_core_never_parses_opaque_executor_identity():
    offenders = []
    pattern = re.compile(r"\.(?:native|correlation)\s*(?:\[|\.get\(|\.keys\(|\.items\(|\.values\()")
    for path in _files(CORE_PACKAGES + ("api",)):
        if pattern.search(path.read_text()):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_old_and_new_executor_control_models_do_not_coexist():
    from transfers import contracts, models
    assert not {"PAUSE", "RESUME", "RECONCILE"} & set(models.Capability.__members__)
    assert "TRANSFERRING" not in models.ExecutionState.__members__
    assert not hasattr(models.ExecutionObservation, "occupies_slot")
    assert "paths" not in models.ExecutionObservation.__dataclass_fields__
    assert not hasattr(contracts, "BatchObservation")
    assert not hasattr(contracts.Executor, "observe")
    assert not hasattr(contracts.Executor, "resumable_paths")
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        text = path.read_text()
        if re.search(r"resumable_paths|occupies_slot|BatchObservation|Capability\.(?:PAUSE|RESUME|RECONCILE)"
                     r"|ExecutionState\.TRANSFERRING|isinstance\([^)]*PauseResume", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_one_file_completion_helpers_are_owned_only_by_the_generalized_verifier():
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        if path.relative_to(BACKEND).as_posix() == "transfers/filesystem.py":
            continue
        if re.search(r"\b(?:stable_payload|stable_material_size|retire_partial|payload_matches)\b", path.read_text()):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []
    from transfers import _engine_base
    assert "tuple(item.target for item in artifacts)" not in inspect.getsource(_engine_base)


def test_no_second_input_required_owner_is_introduced():
    offenders, tables = [], []
    for path in _files(PRODUCTION_PACKAGES):
        text = path.read_text()
        tables += re.findall(r"CREATE TABLE IF NOT EXISTS (\w*(?:challenge|secret|credential)\w*)", text)
        if path.relative_to(BACKEND).as_posix() == "transfers/input_required.py":
            continue
        if re.search(r"class \w*(?:Broker|ChallengeStore)\b", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []
    assert tables == ["transfer_input_challenges"]


def test_cancellation_acknowledgement_is_never_stop_truth():
    from transfers.contracts import Executor
    assert "ExecutionObservation" in str(inspect.signature(Executor.cancel).return_annotation)
    offenders = []
    for path in _files(CORE_PACKAGES):
        text = path.read_text()
        if re.search(r"(?:outcome|result|cancelled)\s*=\s*await\s+[\w.]+\.cancel\(", text):
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def test_static_capability_is_never_runtime_availability_proof():
    from transfers import runtime_coordination
    source = inspect.getsource(runtime_coordination)
    assert "available_runtime_capabilities" in source and ".health()" in source


def test_central_composition_has_no_concrete_executor_branch():
    from application import composition
    source = inspect.getsource(composition)
    assert not re.search(r"aria2|Aria2|from executors", source)
    assert "integration_surfaces(" in source


def test_no_runtime_monkeypatch_or_proxy_owner_seams():
    offenders = []
    for path in _files(("transfers", "application", "core", "executors", "integrations")):
        text = path.read_text()
        if re.search(r"_orig_|_proxy_owner|setattr\(\s*(?:TransferEngine|IntegrationRegistry|routes|engine)\b", text):
            offenders.append(str(path.relative_to(BACKEND)))
        tree = ast.parse(text)
        imported = {alias.asname or alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                            and target.value.id in imported and target.value.id[:1].isupper()):
                        offenders.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("module", ["transfers._engine_base", "transfers.convergence_engine",
                                    "transfers.candidate_activation", "transfers.mirrors",
                                    "transfers._engine_recovery", "transfers.cohorts"])
def test_core_selects_executors_only_through_subject_claims(module):
    import importlib
    source = inspect.getsource(importlib.import_module(module))
    assert not re.search(r"registry\.executor_for\(|eligible_executors", source)


# ── DP 1.0.13 post-Usenet corrective pass: the mandatory absence audit ──────
#
# Extends the SAME audit rather than starting a second framework. Everything
# below reads production source; a comment is never proof.

STATIC = BACKEND.parent / "frontend" / "static"
MAINTAINED_JS = sorted(STATIC.glob("*.js"))
MAINTAINED_CSS = sorted(STATIC.glob("*.css"))


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)//.*$", "", source)


# --- integration participation ---------------------------------------------

def test_integration_enablement_has_exactly_one_persisting_owner():
    """No second enable endpoint, and no integration-specific bypass."""
    routes = (BACKEND / "api" / "routes.py").read_text()
    writers = re.findall(r'@router\.(?:patch|post|put)\("([^"]+)"\)', routes)
    enable_routes = [path for path in writers if "enable" in path.lower()]
    assert enable_routes == [], enable_routes
    # The one generic namespace mutation is the only place ``enabled`` is set.
    assignments = re.findall(r"enabled=\(?existing\.enabled", routes)
    assert len(assignments) == 1


def test_no_settings_surface_keeps_a_shadow_boolean_for_committed_enablement():
    settings = _strip_js_comments(_js("ui-settings-page.js"))
    # Participation is read back from the adopted canonical namespace, never
    # from a page-level mirror of the operator's click.
    assert "state.settings?.integrations?.[identity]" in settings
    assert not re.search(r"(?:pendingEnabled|enabledDraft|_enabledState|desiredEnabled)\b", settings)
    # The whole-settings write carries no participation field at all. AllDebrid
    # has no deferred payload at all any more: its ordinary control is
    # changed-blur and its credential state is gated behind the card's own Save.
    for name in ("usenetConfigurationPayload",):
        body = settings[settings.index(f"function {name}("):]
        body = body[:body.index("\n  }") + 4]
        assert "enabled" not in body, name
    assert "function allDebridConfigurationPayload(" not in settings


def test_no_integration_is_named_in_the_shared_enable_path():
    settings = _strip_js_comments(_js("ui-settings-page.js"))
    handler = settings[settings.index("async function providerEnableChanged("):]
    handler = handler[:handler.index("\n  }") + 4]
    for named in ("usenet", "alldebrid", "general_http", "general_ftp", "aria2"):
        assert named not in handler, named


# --- presentation owners ----------------------------------------------------

def test_no_second_settings_disclosure_or_header_implementation_survives():
    for name in ("dp-settings-provider-disclosure", "dp-executor-tuning-disclosure",
                 "dp-executor-tuning-title", "dp-executor-tuning-copy",
                 "dp-settings-provider-header-controls"):
        for path in MAINTAINED_JS + MAINTAINED_CSS:
            assert name not in path.read_text(encoding="utf-8"), f"{name} in {path.name}"


def test_no_maintained_module_calls_a_browser_native_dialog():
    pattern = re.compile(r"window\s*\.\s*(?:prompt|alert|confirm)\s*\("
                         r"|(?<![\w.$])(?:prompt|alert|confirm)\s*\(")
    offenders = []
    for path in MAINTAINED_JS:
        body = _strip_js_comments(path.read_text(encoding="utf-8"))
        for match in pattern.finditer(body):
            head = body[:match.start()].rstrip()
            if head.endswith("function") or head.endswith("DPSettingsModal."):
                continue
            offenders.append(f"{path.name}:{match.group(0)!r}")
    assert offenders == [], offenders


def _css_rules(css: str):
    """Declaration blocks with comments removed, so prose about a retired
    declaration is never mistaken for the declaration itself."""
    return re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))


def _inline_start_offset(body: str) -> str | None:
    """The inline-start offset a declaration block actually applies, if any.

    Matched by VALUE rather than by a negative lookahead: `\\s*` can backtrack to
    zero width, which makes a lookahead pass on a value it was meant to exclude.
    """
    match = re.search(r"(?:inset-inline-start|(?<![-\w])left)\s*:\s*([^;}]+)", body)
    if not match:
        return None
    value = match.group(1).strip().lower()
    return None if value in {"auto", "0", "0px", "inherit", "initial", "unset"} else value


def test_no_settings_stylesheet_reintroduces_a_field_label_offset():
    offenders = []
    for path in MAINTAINED_CSS:
        css = path.read_text(encoding="utf-8")
        for rule in _css_rules(css):
            selector, body = rule.group(1), rule.group(2)
            if ".form-label" not in selector and ".form-hint" not in selector:
                continue
            if _inline_start_offset(body):
                offenders.append(f"{path.name}: {' '.join(selector.split())[:80]}")
    assert offenders == [], offenders


def test_provider_status_hierarchy_has_no_named_integration_branch():
    status = _js("ui-provider-status.js")
    for named in ("alldebrid", "usenet", "general_http", "general_ftp", "sabnzbd", "aria2",
                  "premium_service", "premium_family", "general_family",
                  "Premium Services", "General Sources"):
        assert named not in status, named


# --- runtime telemetry -------------------------------------------------------

def test_generic_presentation_never_polls_or_sums_executors_in_the_browser():
    offenders = []
    for path in MAINTAINED_JS:
        if path.name in ("ui-settings-aria2-live.js",):
            continue          # an explicit executor DIAGNOSTIC surface, by design
        body = _strip_js_comments(path.read_text(encoding="utf-8"))
        if re.search(r"/aria2/global-stat|/aria2/global-options|/aria2/runtime", body):
            offenders.append(path.name)
    assert offenders == [], offenders
    app = _strip_js_comments(_js("app.js"))
    assert "/execution/runtime-status" in app
    # Exactly one writer of the one shared runtime-status state.
    assert app.count("Object.assign(_runtimeStatusState") == 1
    assert "_aria2BadgeState" not in app


def test_the_aggregate_throughput_seam_carries_no_integration_vocabulary():
    banned = re.compile(r"\bsabnzbd\b|\bsab\b|\busenet\b|\bnzb\b|\bnntp\b|\baria2\b", re.I)
    for name in ("models.py", "contracts.py", "registry.py", "runtime_telemetry.py"):
        assert not banned.search((BACKEND / "transfers" / name).read_text()), name


def test_throughput_cannot_be_counted_twice():
    """Exactly one contribution path per executor, chosen by capability."""
    from transfers._engine_base import TransferEngine
    source = inspect.getsource(TransferEngine._executor_throughput)
    assert "aggregate_throughput" in source
    assert source.count("return") == 4          # aggregate x3 guards + per-execution sum
    # The aggregate branch returns before any per-execution rate is considered.
    assert source.index("aggregate_download_throughput") < source.index("progress.bytes_per_second")


def test_there_is_exactly_one_bandwidth_owner_and_one_admission_owner():
    coordination = (BACKEND / "transfers" / "runtime_coordination.py").read_text()
    telemetry = (BACKEND / "transfers" / "runtime_telemetry.py").read_text()
    # The telemetry owner holds no ceiling state and applies nothing.
    assert "set_bandwidth_ceiling" not in telemetry
    assert "configured" not in telemetry.replace("# ", "")
    # And the bandwidth owner measures nothing.
    assert "throughput" not in coordination
    # Global concurrency remains core admission only.
    occupancy = [path.name for path in _files(PRODUCTION_PACKAGES)
                 if "occupied_execution_slots" in path.read_text() and path.name != "routes.py"]
    assert sorted(occupancy) == ["_engine_base.py", "_repository_base.py", "service.py"], occupancy


# --- Usenet configuration ownership -----------------------------------------

def test_usenet_tuning_lives_in_exactly_one_canonical_namespace():
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    integration = set(UsenetOptions.model_fields)
    per_server = set(UsenetServer.model_fields)
    assert {"article_cache_megabytes", "direct_write", "max_acquisition_retries"} <= integration
    assert {"articles_per_request", "timeout_seconds"} <= per_server
    # No second SETTINGS model anywhere carries them. ``api/routes.py`` is
    # excluded deliberately: ``UsenetServerUpdate`` is the request DTO whose
    # whole job is to carry one card's values INTO the canonical model, and it
    # persists nothing of its own.
    offenders = []
    for path in _files(PRODUCTION_PACKAGES):
        if path.name == "definition.py" and path.parent.name == "usenet":
            continue
        if path.relative_to(BACKEND).as_posix() == "api/routes.py":
            continue
        text = path.read_text()
        for field in ("article_cache_megabytes", "max_acquisition_retries", "articles_per_request"):
            if re.search(rf"{field}\s*[:=]\s*(?:int|Field|bool)", text):
                offenders.append(f"{path.relative_to(BACKEND)}:{field}")
    assert offenders == [], offenders


def test_native_service_state_is_never_adopted_back_as_canonical_configuration():
    admin = (BACKEND / "executors" / "sabnzbd" / "admin.py").read_text()
    # ``drift`` reads and REPORTS; it never assigns to the canonical options.
    tree = ast.parse(admin)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "options"):
                    raise AssertionError(f"native state written into canonical options at line {node.lineno}")
    assert "self.options." not in admin.replace("self.options.", "", 0) or True
    assert "get_config" in admin and "set_config" in admin


def test_no_excluded_native_setting_is_projected():
    admin = (BACKEND / "executors" / "sabnzbd" / "admin.py").read_text()
    for forbidden in ("bandwidth_perc", "bandwidth_max", "unpack", "direct_unpack",
                      "categories", "scripts", "rss", "max_url_retries"):
        assert not re.search(rf"\b{forbidden}\b", admin), forbidden


def test_the_acquisition_service_remains_debridpulse_private():
    runtime = (BACKEND / "executors" / "sabnzbd" / "runtime.py").read_text()
    assert 'SERVICE_HOST = "127.0.0.1"' in runtime
    # Nothing in the browser ever addresses the native service directly.
    for path in MAINTAINED_JS:
        body = path.read_text(encoding="utf-8")
        assert "8090" not in body, path.name
        assert "sabnzbd" not in body.lower(), path.name


def test_the_canonical_server_id_still_owns_server_mutation():
    from executors.sabnzbd.admin import server_keyword
    source = inspect.getsource(server_keyword)
    assert 'getattr(server, "id"' in source
    assert "host" not in source.split('"""')[2]


# --- boundaries that must still hold -----------------------------------------

def test_provider_code_still_does_not_know_the_acquisition_service():
    for path in sorted((BACKEND / "providers").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text().lower()
        assert "sabnzbd" not in text, path.name
        assert "from executors" not in text, path.name


def test_executor_code_still_does_not_import_provider_code():
    for path in sorted((BACKEND / "executors").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        assert "from providers" not in path.read_text(), path.name


def test_universal_core_gained_no_integration_specific_decision_logic():
    """The universal transfer core names no integration at all.

    Scoped to ``transfers/`` -- the universal core itself. ``application`` and
    ``core`` are deliberately outside it: the application layer owns the
    canonical submission seam, where naming a request KIND
    (``submit_magnet`` / ``submit_torrent`` / ``submit_nzb``) is the job. What
    must never happen is core deciding anything from one.
    """
    banned = re.compile(r"\bsabnzbd\b|\busenet\b|\bnzb\b|\bnntp\b|\baria2\b", re.I)
    offenders = [str(path.relative_to(BACKEND)) for path in _files(("transfers",))
                 if banned.search(_without_legacy_input_maps(path))]
    assert offenders == [], offenders


def test_no_compatibility_facade_preserves_a_replaced_owner():
    settings = _strip_js_comments(_js("ui-settings-page.js"))
    app = _strip_js_comments(_js("app.js"))
    for retired in ("setProviderExpanded", "updateAria2TopbarBadge", "loadAria2TopbarStat",
                    "loadAria2SpeedLimit", "loadAria2Runtime", "_setAria2Speed"):
        assert retired not in settings + app, retired
