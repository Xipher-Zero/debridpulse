"""DP 1.0.12 canonical architecture correction, Workstream C.

Scoped namespace mutation surfaces (specification section 9.5): executor
controls must no longer submit a whole stale ``Settings`` snapshot.
``PATCH /transfer-policy`` and ``PATCH /integrations/{integration_id}/configuration``
each reload only their own namespace, preserve every unrelated setting
(including sibling integrations), and use the ordinary
``application.application_operation()`` admission -- never
``ApplicationMaintenanceGate`` merely because the value is persisted
(specification sections 2.6, 6).

Existing coverage this file does not duplicate: the neutral bandwidth
surface itself (``test_executor_runtime_limits.py``), the compatibility-edge
``/aria2/global-options`` admission contract (``test_stage10_settings_admission.py``),
general architecture guardrails (``test_canonical_runtime_architecture.py``).
"""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api import routes
from core.config import AppSettings
from executors.aria2.definition import definition as aria2_definition
from integrations.configuration import normalize_settings
from integrations.definition import IntegrationSettings
from providers.alldebrid.definition import definition as alldebrid_definition
from providers.general_http.definition import definition as general_http_definition
from transfers.settings import TransferSettings


DEFINITIONS = (alldebrid_definition, general_http_definition, aria2_definition)


def _application(current, *, validate_configuration=None, aria2_admin=None):
    # A single stable admin object (not a fresh one per ``integration_admin``
    # call) so tests can assert on its ``apply_memory_tuning`` mock.
    admin = aria2_admin if aria2_admin is not None else SimpleNamespace(apply_memory_tuning=AsyncMock())
    return SimpleNamespace(
        definitions=DEFINITIONS,
        application_operation=lambda: _noop(),
        configuration_admission=lambda: _raise_if_entered(),
        configure=MockConfigure(),
        reconcile_executions=AsyncMock(),
        integration_admin=lambda _identity: admin,
        validate_configuration=validate_configuration or AsyncMock(),
    )


@asynccontextmanager
async def _noop():
    yield


@asynccontextmanager
async def _raise_if_entered():
    raise AssertionError("configuration_admission() (ApplicationMaintenanceGate) must never be entered by a scoped route")
    yield  # pragma: no cover


class MockConfigure:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _settings_with_full_integrations():
    return AppSettings(
        integrations={
            "alldebrid": IntegrationSettings(options={"api_key": "secret-key"}),
            "general_http": IntegrationSettings(options={}),
            # A value an earlier release stored and no current option names.
            "aria2": IntegrationSettings(options={"mode": "builtin", "split": 16, "disk_cache": "64M"}),
        },
        transfer_policy=TransferSettings(max_concurrent_executions=3, execution_retry_count=3),
    )


# --------------------------------------------------------------------------- #
# PATCH /transfer-policy
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_transfer_policy_reports_canonical_namespace():
    current = _settings_with_full_integrations()
    application = _application(current)
    with patch("api.routes.get_settings", return_value=current):
        result = await routes.get_transfer_policy_ep(application=application)
    assert result["ok"] is True
    assert result["max_concurrent_executions"] == 3
    assert result["execution_retry_count"] == 3


@pytest.mark.asyncio
async def test_patch_transfer_policy_updates_only_requested_field():
    current = _settings_with_full_integrations()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings", side_effect=lambda cfg: saved.__setitem__("applied", cfg)):
        result = await routes.patch_transfer_policy(
            routes.TransferPolicyUpdate(max_concurrent_executions=9), application=application,
        )
    assert result["max_concurrent_executions"] == 9
    # Untouched retry policy is preserved, not reset to model defaults.
    assert result["execution_retry_count"] == 3
    assert saved["cfg"].transfer_policy.max_concurrent_executions == 9
    # The legacy alias is migration input only (specification section 9.2): it
    # is not a field of the settings document, so a canonical save cannot
    # regenerate it as a persisted mirror.
    assert "max_concurrent_downloads" not in saved["cfg"].model_dump()
    # Sibling integration namespaces are untouched.
    assert saved["cfg"].integrations["alldebrid"].options["api_key"] == "secret-key"
    assert saved["cfg"].integrations["aria2"].options["split"] == 16
    assert application.configure.calls == 1
    application.reconcile_executions.assert_awaited_once()
    # Gate 9 revision-5 rejection finding 4: a concurrency change projects
    # into the running aria2 daemon's native
    # ``max-concurrent-downloads`` through the SAME executor-owned
    # administration entry point periodic housekeeping already uses -- not a
    # second native-option pipeline.
    application.integration_admin("aria2").apply_memory_tuning.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_transfer_policy_reconfigures_engine_but_skips_dispatch_nudge_for_retry_only_change():
    """Gate 9 revision-3 rejection finding 4: a retry-only PATCH must still
    reconfigure the running engine (``application.configure()`` is the one
    place ``execution_retry_count``/``execution_retry_delay_seconds`` become
    the universal recovery engine's live retry policy) -- but must NOT pay
    for the capacity-triggered dispatch nudge (``reconcile_executions``),
    which exists only to use newly available concurrency slots that a
    retry-only change never creates."""
    current = _settings_with_full_integrations()
    application = _application(current)
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings"), patch("api.routes.apply_settings"):
        await routes.patch_transfer_policy(
            routes.TransferPolicyUpdate(execution_retry_count=5), application=application,
        )
    assert application.configure.calls == 1
    application.reconcile_executions.assert_not_awaited()
    # A retry-only change must not touch native aria2 options at all --
    # nothing about the concurrency projection is relevant here.
    application.integration_admin("aria2").apply_memory_tuning.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_transfer_policy_reports_native_concurrency_apply_failure_truthfully():
    """Specification section 2.7: an API success must not imply that a
    failed native apply succeeded. When the daemon rejects the
    projected native concurrency option, the route must report it as
    ``last_apply_error`` rather than silently swallowing it -- the durable
    desired value (the actual policy authority) is still persisted either
    way."""
    current = _settings_with_full_integrations()
    admin = SimpleNamespace(apply_memory_tuning=AsyncMock(side_effect=RuntimeError("daemon unreachable")))
    application = _application(current, aria2_admin=admin)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_transfer_policy(
            routes.TransferPolicyUpdate(max_concurrent_executions=9), application=application,
        )
    assert result["ok"] is False
    assert result["last_apply_error"]
    assert result["max_concurrent_executions"] == 9
    # Durable desired state is the actual scheduler authority and is not
    # rewritten back because the native projection failed.
    assert saved["cfg"].transfer_policy.max_concurrent_executions == 9


@pytest.mark.asyncio
async def test_patch_transfer_policy_rejects_empty_body():
    application = _application(_settings_with_full_integrations())
    with pytest.raises(routes.HTTPException) as excinfo:
        await routes.patch_transfer_policy(routes.TransferPolicyUpdate(), application=application)
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_transfer_policy_rejects_out_of_bounds_value():
    application = _application(_settings_with_full_integrations())
    with patch("api.routes.load_settings", return_value=_settings_with_full_integrations()):
        with pytest.raises(routes.HTTPException) as excinfo:
            await routes.patch_transfer_policy(
                routes.TransferPolicyUpdate(max_concurrent_executions=999), application=application,
            )
    assert excinfo.value.status_code == 400


def test_transfer_policy_route_never_acquires_configuration_admission():
    source = (routes.__file__ and __import__("pathlib").Path(routes.__file__)).read_text(encoding="utf-8")
    start = source.index("async def patch_transfer_policy(")
    end = source.index("\n\n@router.", start)
    body = source[start:end]
    assert "application.application_operation()" in body
    assert "configuration_admission" not in body


# --------------------------------------------------------------------------- #
# PATCH /integrations/{integration_id}/configuration
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_get_integration_configuration_reports_public_namespace():
    current = _settings_with_full_integrations()
    application = _application(current)
    with patch("api.routes.get_settings", return_value=current):
        result = await routes.get_integration_configuration("aria2", application=application)
    assert result["ok"] is True
    assert result["options"]["split"] == 16


@pytest.mark.asyncio
async def test_patch_integration_configuration_merges_only_supplied_options():
    current = _settings_with_full_integrations()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "aria2", routes.IntegrationConfigurationUpdate(options={"split": 32}), application=application,
        )
    assert result["options"]["split"] == 32
    # Untouched aria2 option preserved, not reset to the model default.
    assert result["options"]["disk_cache"] == "64M"
    assert saved["cfg"].integrations["aria2"].options["split"] == 32
    assert saved["cfg"].integrations["aria2"].options["disk_cache"] == "64M"
    # Sibling integrations untouched.
    assert saved["cfg"].integrations["alldebrid"].options["api_key"] == "secret-key"
    assert application.configure.calls == 1


def _settings_with_configured_integration_secret():
    current = _settings_with_full_integrations()
    current.integrations = {
        **current.integrations,
        "alldebrid": IntegrationSettings(options={"api_key": "configured-secret", "rate_limit_per_minute": 60}),
    }
    return current


@pytest.mark.asyncio
async def test_patch_integration_configuration_blank_secret_preserves_existing_value():
    """Gate 9 revision-3 rejection finding 2, specification section 9.5: the
    UI's existing contract is that a blank ALREADY-CONFIGURED secret control
    means "keep current" -- ``allDebridConfigurationPayload()`` always sends
    ``api_key: ""`` for it. An ordinary scoped Save (no explicit
    ``clear_secrets``) supplying a blank secret alongside an unrelated
    option must not erase the stored value."""
    # ``load_settings`` returns a FRESH COPY on every call, matching real
    # ``core.config.load_settings()`` (which always parses a brand new
    # ``AppSettings`` from disk) -- unlike ``get_settings()``, which returns
    # the live cached object. Returning the SAME shared object from both
    # (as a naive mock would) would let the route's in-place mutation of its
    # local ``current`` variable silently "leak" into ``previous`` before
    # ``normalize_settings`` ever reads it, masking exactly the secret-
    # preservation bug this test exists to catch.
    current = _settings_with_configured_integration_secret()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", side_effect=lambda: current.model_copy(deep=True)), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "alldebrid", routes.IntegrationConfigurationUpdate(options={"rate_limit_per_minute": 90, "api_key": ""}),
            application=application,
        )
    assert result["options"]["rate_limit_per_minute"] == 90
    assert result["options"]["api_key_configured"] is True
    assert saved["cfg"].integrations["alldebrid"].options["api_key"] == "configured-secret"


@pytest.mark.asyncio
async def test_patch_integration_configuration_explicit_clear_secrets_erases_value():
    """The SAME blank value, but with the secret named in ``clear_secrets``,
    must still erase it -- blank-preserves is not a blanket immunity from an
    explicit user-requested clear."""
    current = _settings_with_configured_integration_secret()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", side_effect=lambda: current.model_copy(deep=True)), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "alldebrid", routes.IntegrationConfigurationUpdate(options={"api_key": ""}, clear_secrets=["api_key"]),
            application=application,
        )
    assert result["options"]["api_key_configured"] is False
    assert saved["cfg"].integrations["alldebrid"].options["api_key"] == ""


@pytest.mark.asyncio
async def test_patch_integration_configuration_nonblank_secret_replaces_existing_value():
    """A genuine new value is never shadowed by preservation -- preservation
    only applies to a blank/omitted supplied value."""
    current = _settings_with_configured_integration_secret()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", side_effect=lambda: current.model_copy(deep=True)), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "alldebrid", routes.IntegrationConfigurationUpdate(options={"api_key": "rotated-secret"}),
            application=application,
        )
    assert result["options"]["api_key_configured"] is True
    assert saved["cfg"].integrations["alldebrid"].options["api_key"] == "rotated-secret"


@pytest.mark.asyncio
async def test_patch_integration_configuration_unrelated_field_update_does_not_touch_transfer_policy():
    current = _settings_with_full_integrations()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        await routes.patch_integration_configuration(
            "aria2", routes.IntegrationConfigurationUpdate(options={"split": 32}), application=application,
        )
    assert saved["cfg"].transfer_policy.max_concurrent_executions == 3


@pytest.mark.asyncio
async def test_patch_integration_configuration_unknown_integration_is_404():
    application = _application(_settings_with_full_integrations())
    with pytest.raises(routes.HTTPException) as excinfo:
        await routes.patch_integration_configuration(
            "not-a-real-integration", routes.IntegrationConfigurationUpdate(options={}), application=application,
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_integration_configuration_rejects_invalid_option_value():
    current = _settings_with_full_integrations()
    application = _application(current)
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current):
        with pytest.raises(routes.HTTPException) as excinfo:
            await routes.patch_integration_configuration(
                "aria2", routes.IntegrationConfigurationUpdate(options={"split": "not-a-number"}), application=application,
            )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_patch_integration_configuration_honors_the_existing_reference_guard():
    """Specification section 6: a scoped change still goes through the SAME
    proven-invariant check the whole-settings route already uses
    (``ApplicationService.validate_configuration``) -- never a blanket
    maintenance wait instead."""
    current = _settings_with_full_integrations()

    async def reject(_previous, _clean):
        raise ValueError("Finish or remove existing resources before changing this integration")

    application = _application(current, validate_configuration=reject)
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current):
        with pytest.raises(routes.HTTPException) as excinfo:
            await routes.patch_integration_configuration(
                "aria2", routes.IntegrationConfigurationUpdate(options={"split": 8}),
                application=application,
            )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_aria2_settings_carry_no_daemon_topology_and_tuning_round_trips():
    """GET exposes exactly the current aria2 schema; a tuning-only PATCH needs no
    topology and never writes a value the schema does not define back to
    storage or the response, even when the stored namespace still holds some."""
    from test_executor_configuration_ownership import CURRENT_ARIA2_OPTIONS
    current = _settings_with_full_integrations()
    public = routes._public_settings(normalize_settings(current, DEFINITIONS), DEFINITIONS)
    assert set(public["integrations"]["aria2"]["options"]) == CURRENT_ARIA2_OPTIONS
    aria2_projection = {name for name in public["compatibility_fields"]
                        if name.startswith("aria2_") and name[len("aria2_"):] in public["integrations"]["aria2"]["options"]}
    assert aria2_projection == {f"aria2_{option}" for option in CURRENT_ARIA2_OPTIONS}

    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"), \
         patch("api.routes.aria2_runtime", SimpleNamespace(ensure_started=AsyncMock(), restart=AsyncMock(), stop=AsyncMock())):
        result = await routes.patch_integration_configuration(
            "aria2", routes.IntegrationConfigurationUpdate(options={"split": 8, "disk_cache": "32M"}),
            application=application,
        )
    stored = saved["cfg"].integrations["aria2"].options
    assert set(stored) == CURRENT_ARIA2_OPTIONS
    assert stored["split"] == 8 and stored["disk_cache"] == "32M"
    assert set(result["options"]) == CURRENT_ARIA2_OPTIONS


def test_integration_configuration_route_never_acquires_configuration_admission():
    source = __import__("pathlib").Path(routes.__file__).read_text(encoding="utf-8")
    start = source.index("async def patch_integration_configuration(")
    end = source.index("\n\n@router.", start)
    body = source[start:end]
    assert "application.application_operation()" in body
    assert "configuration_admission" not in body


# --------------------------------------------------------------------------- #
# Concurrent namespace writes (specification section 13.8): no namespace
# loses another namespace's newer value; no stale whole-snapshot replacement.
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_sequential_transfer_policy_and_integration_writes_do_not_clobber_each_other():
    """Simulates the ordering a race would produce: each scoped write reloads
    the CURRENT durable settings (``load_settings()``) rather than operating
    on a snapshot captured before the other write landed."""
    store = {"cfg": _settings_with_full_integrations()}

    def fake_load():
        return store["cfg"]

    def fake_save(cfg):
        store["cfg"] = cfg

    application = _application(store["cfg"])
    with patch("api.routes.get_settings", side_effect=fake_load), \
         patch("api.routes.load_settings", side_effect=fake_load), \
         patch("api.routes.save_settings", side_effect=fake_save), \
         patch("api.routes.apply_settings"):
        await routes.patch_transfer_policy(
            routes.TransferPolicyUpdate(max_concurrent_executions=11), application=application,
        )
        await routes.patch_integration_configuration(
            "aria2", routes.IntegrationConfigurationUpdate(options={"split": 40}), application=application,
        )

    final = store["cfg"]
    assert final.transfer_policy.max_concurrent_executions == 11
    assert final.integrations["aria2"].options["split"] == 40
    assert final.integrations["alldebrid"].options["api_key"] == "secret-key"


@pytest.mark.asyncio
async def test_concurrent_integration_configuration_writes_do_not_lose_updates():
    """Gate 9 revision-2 rejection finding, specification section 13.8: a
    REAL race, not merely one simulated ordering -- writer A's
    ``load_settings()``, its awaited ``validate_configuration()``, and its
    ``save_settings()`` must not interleave with writer B's own
    load-modify-save such that B's save silently erases A's change (classic
    lost update: A loads X, B loads X before A saves, A saves X+A, B saves
    X+B, A's change is gone). ``application.application_operation()`` here
    is a bare no-op context manager (``_application()``'s ``_noop()``) --
    admission accounting alone never serializes this -- so this proves
    ``core.config.config_write_lock()`` itself is what prevents the lost
    update, by forcing B's ``load_settings()`` to block until A's entire
    critical section (including its save) has completed.

    ``fake_load`` returns a FRESH COPY on every call, matching real
    ``core.config.load_settings()`` (which always parses a brand new
    ``AppSettings`` from disk) -- returning the same shared mutable object
    every call would let one writer's in-place mutation silently "leak" into
    the other's read even with no lock at all, masking the exact race this
    test exists to catch."""
    store = {"cfg": _settings_with_full_integrations()}

    def fake_load():
        return store["cfg"].model_copy(deep=True)

    def fake_save(cfg):
        store["cfg"] = cfg

    validate_calls = []

    async def slow_validate(_previous, _clean):
        # A genuine ``await`` inside the critical section -- the exact shape
        # of yield point a real race needs to interleave two concurrent
        # requests without a serializing lock.
        validate_calls.append(_clean)
        await asyncio.sleep(0.01)

    application = _application(store["cfg"], validate_configuration=slow_validate)
    with patch("api.routes.get_settings", side_effect=fake_load), \
         patch("api.routes.load_settings", side_effect=fake_load), \
         patch("api.routes.save_settings", side_effect=fake_save), \
         patch("api.routes.apply_settings"):
        await asyncio.gather(
            routes.patch_integration_configuration(
                "aria2", routes.IntegrationConfigurationUpdate(options={"split": 40}), application=application,
            ),
            routes.patch_integration_configuration(
                "alldebrid", routes.IntegrationConfigurationUpdate(options={"api_key": "rotated-key"}), application=application,
            ),
        )

    # Both concurrent writers actually ran (proves this exercised real
    # interleaving opportunity, not a fixture that only ever runs one path).
    assert len(validate_calls) == 2
    final = store["cfg"]
    assert final.integrations["aria2"].options["split"] == 40
    assert final.integrations["alldebrid"].options["api_key"] == "rotated-key"


# --------------------------------------------------------------------------- #
# PATCH /execution/runtime-limits vs. a stale whole-settings PUT
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_stale_whole_settings_put_does_not_clobber_a_newer_runtime_limit_patch():
    """Gate 9 revision-4 rejection finding 3: ``aria2_max_download_limit`` is
    a one-way legacy migration INPUT only (specification section 9.2,
    ``transfers.runtime_limits.normalize_runtime_limits``) -- once
    ``execution_runtime_limits`` is the canonical authority, a whole-settings
    PUT built from a snapshot fetched BEFORE a scoped runtime-limit PATCH
    landed must not resurrect the old legacy value. The frontend no longer
    includes ``aria2_max_download_limit`` in the submitted body (Gate 9
    revision-4 correction, ``ui-settings-page.js``'s
    ``RUNTIME_LIMIT_CANONICAL_LEGACY_FIELDS``), so a real browser payload's
    ``model_fields_set`` never contains it -- reproduced here by excluding it
    the same way ``AppSettings.model_dump(exclude=...)`` would."""
    store = {"cfg": _settings_with_full_integrations()}

    def fake_load():
        return store["cfg"].model_copy(deep=True)

    def fake_save(cfg):
        store["cfg"] = cfg

    application = _application(store["cfg"])
    application.configuration_admission = lambda: _noop()
    with patch("api.routes.get_settings", side_effect=fake_load), \
         patch("api.routes.load_settings", side_effect=fake_load), \
         patch("api.routes.save_settings", side_effect=fake_save), \
         patch("api.routes.apply_settings"), \
         patch.object(routes.aria2_runtime, "ensure_started", AsyncMock()), \
         patch.object(routes.aria2_runtime, "restart", AsyncMock()):
        # Browser B: scoped PATCH raises the canonical runtime limit.
        await routes.patch_execution_runtime_limits(
            {"max_download_bytes_per_second": 10_000_000}, application=application,
        )
        # Browser A: whole-settings PUT built the way the real (fixed)
        # frontend builds it -- ``execution_runtime_limits`` is ALWAYS
        # stripped from the whole-settings body (``nonAuthPayload()`` already
        # did this before this correction) and, after this correction,
        # ``aria2_max_download_limit`` is stripped too, so neither key nor
        # its ``model_fields_set`` membership carries A's stale pre-B value
        # into the PUT at all.
        stale_snapshot = store["cfg"].model_dump(
            exclude={"aria2_max_download_limit", "execution_runtime_limits"},
        )
        await routes.update_settings(
            routes.SettingsUpdate(**stale_snapshot), application=application,
        )

    assert store["cfg"].execution_runtime_limits.max_download_bytes_per_second == 10_000_000
