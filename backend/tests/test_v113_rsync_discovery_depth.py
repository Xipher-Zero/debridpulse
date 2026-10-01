"""DP 1.0.13 rsync as a first-class consumer of the neutral ``DiscoveryDepth``.

Directory Depth is the general rsync PROVIDER's discovery policy
(``integrations.general_rsync.options.directory_depth``, default "all
subdirectories" -- exactly the whole-tree behavior every existing
installation has). The provider issues it as the neutral depth in its
``DiscoveryRequest``; the rsync EXECUTOR alone realizes it, with rsync's own
sender-side filter engine: a directory below the requested depth is excluded
by the invocation itself, so the sender never opens it -- nothing is listed
and then discarded by DebridPulse.

Everything native runs the installed rsync binary against a real rsync
daemon and a real ``rsync --server`` behind an SSH origin.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from rsync_origins import RsyncDaemon, RsyncSshOrigin, write_tree
from test_v113_rsync_executor import (  # noqa: F401
    PASSWORD, USER, _candidate, _executor, _identity, _loopback_destinations, _password, spawned,
)
from test_v113_transport_evidence_sampling import guard_for
from transfers.errors import Category, TransferError
from transfers.models import (
    DiscoveryDepth, DiscoveryLimits, ExecutionSubject, InputMethod, RemoteObjectKind, TransferRequest,
)

# Files at every level below the discovered directory "tree":
#   level 0: r.bin              level 1: l1/f1.bin
#   level 2: l1/l2/f2.bin       level 3: l1/l2/l3/f3.bin      level 4: l1/l2/l3/l4/f4.bin
# plus a sibling first-level directory, so a level is never one path only.
FILES = {"r.bin": b"r", "l1/f1.bin": b"f1", "l1/l2/f2.bin": b"f2-", "l1/l2/l3/f3.bin": b"f3--",
         "l1/l2/l3/l4/f4.bin": b"f4---", "side/s1.bin": b"s1"}
EXPECTED = {
    DiscoveryDepth.CURRENT: {"r.bin"},
    DiscoveryDepth.of(1): {"r.bin", "l1/f1.bin", "side/s1.bin"},
    DiscoveryDepth.of(2): {"r.bin", "l1/f1.bin", "side/s1.bin", "l1/l2/f2.bin"},
    DiscoveryDepth.of(3): {"r.bin", "l1/f1.bin", "side/s1.bin", "l1/l2/f2.bin", "l1/l2/l3/f3.bin"},
    DiscoveryDepth.UNLIMITED: set(FILES),
}


def _tree(root):
    write_tree(root, FILES)
    # A directory whose contents lie at level 4: no finite depth up to 3 may
    # ever open it. It is unreadable, so a sender that descended into it would
    # fail the listing -- the proof that pruning happens in the invocation.
    locked = root / "l1" / "l2" / "l3" / "locked"
    locked.mkdir()
    (locked / "never.bin").write_bytes(b"never")
    locked.chmod(0)
    return locked


@pytest_asyncio.fixture
async def daemon(tmp_path):
    locked = _tree(tmp_path / "srv" / "pub" / "tree")
    write_tree(tmp_path / "srv" / "other", {"o.bin": b"o", "deep/o2.bin": b"o2", "deep/er/o3.bin": b"o3"})
    origin = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv" / "pub"},
                                               "other": {"path": tmp_path / "srv" / "other"}}).start()
    guard = guard_for()
    yield origin, guard
    locked.chmod(0o755)
    origin.stop()
    await guard.stop()


def _paths(result):
    assert result.kind == RemoteObjectKind.DIRECTORY
    return {entry.relative_path for entry in result.entries}


def _listings(spawned):  # noqa: F811
    return [argv for argv, _env, _kwargs in spawned if "--list-only" in argv]


# ── the native translation ───────────────────────────────────────────────────

def test_the_smallest_native_expression_of_each_neutral_depth():
    from executors.rsync.translation import depth_options
    assert depth_options(DiscoveryDepth.CURRENT) == ()
    assert depth_options(DiscoveryDepth.of(1)) == ("-r", "--exclude=/*/*/")
    assert depth_options(DiscoveryDepth.of(2)) == ("-r", "--exclude=/*/*/*/")
    assert depth_options(DiscoveryDepth.of(3)) == ("-r", "--exclude=/*/*/*/*/")
    assert depth_options(DiscoveryDepth.UNLIMITED) == ("-r",)


# ── the real daemon ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("depth", list(EXPECTED), ids=["current", "1", "2", "3", "all"])
async def test_each_depth_lists_exactly_its_levels(tmp_path, daemon, spawned, depth):  # noqa: F811
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    subject = ExecutionSubject.of(_candidate(origin.url("/pub/tree/")))
    expected = EXPECTED[depth]
    if depth.unlimited:
        os.chmod(tmp_path / "srv" / "pub" / "tree" / "l1" / "l2" / "l3" / "locked", 0o755)
        expected = expected | {"l1/l2/l3/locked/never.bin"}
    assert _paths(await executor.discover(subject, depth=depth)) == expected
    (listing,) = [argv for argv in _listings(spawned) if argv[-1].endswith("/tree/")]
    # The filter is the invocation's own; nothing else selects members.
    expected_options = [] if depth == DiscoveryDepth.CURRENT else ["-r"] if depth.unlimited else [
        "-r", "--exclude=/" + "*/" * (depth.levels + 1)]
    assert [item for item in listing if item == "-r" or item.startswith("--exclude")] == expected_options


@pytest.mark.asyncio
@pytest.mark.skipif(os.geteuid() == 0, reason="a root sender reads an unreadable directory")
async def test_deeper_directories_are_pruned_by_the_rsync_invocation_not_discarded(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    subject = ExecutionSubject.of(_candidate(origin.url("/pub/tree/")))
    # Every finite depth succeeds although an unreadable directory lies below
    # it: the sender never opened it.
    for levels in (1, 2, 3):
        assert _paths(await executor.discover(subject, depth=DiscoveryDepth.of(levels))) == EXPECTED[
            DiscoveryDepth.of(levels)]
    # The whole tree does open it -- and a partial listing is never a result.
    with pytest.raises(TransferError):
        await executor.discover(subject, depth=DiscoveryDepth.UNLIMITED)


@pytest.mark.asyncio
async def test_a_daemon_root_applies_the_depth_below_every_named_root(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    root = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/"))), depth=DiscoveryDepth.of(1))
    # Each named root is a listed directory of its own: one level below it.
    assert _paths(root) == {"other/o.bin", "other/deep/o2.bin", "pub/tree/r.bin"}


@pytest.mark.asyncio
async def test_a_file_ignores_the_depth(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    found = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/pub/tree/r.bin"))),
                                    depth=DiscoveryDepth.of(2))
    assert (found.kind, found.expected_bytes) == (RemoteObjectKind.FILE, 1)


@pytest.mark.asyncio
async def test_limits_rsync_does_not_enforce_are_refused(tmp_path):
    executor = _executor(tmp_path, guard_for())
    with pytest.raises(TransferError) as raised:
        await executor.discover(ExecutionSubject.of(_candidate("rsync://h.example/pub/tree/")),
                                depth=DiscoveryDepth.UNLIMITED, limits=DiscoveryLimits(max_files=3))
    assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY


# ── rsync over SSH: the same neutral depth, the same native filter ───────────

@pytest.mark.asyncio
async def test_rsync_over_ssh_realizes_the_same_depth(tmp_path, spawned):  # noqa: F811
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD)).start()
    guard = guard_for()
    locked = _tree(origin.root / "files" / "tree")
    try:
        executor = _executor(tmp_path, guard)
        methods = (InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PRIVATE_KEY)
        confirmed = _password(facts=_identity("rsync-ssh-origin.test", origin.fingerprint()))
        subject = ExecutionSubject.of(_candidate(origin.url(f"{origin.root}/files/tree"), methods=methods))
        for depth in (DiscoveryDepth.CURRENT, DiscoveryDepth.of(1), DiscoveryDepth.of(2)):
            assert _paths(await executor.discover(subject, confirmed, depth=depth)) == EXPECTED[depth]
        assert any("--exclude=/*/*/*/" in argv for argv in _listings(spawned))
    finally:
        locked.chmod(0o755)
        await origin.close()
        await guard.stop()


# ── the provider owns the setting; the default is the whole tree ─────────────

def test_the_rsync_provider_owns_directory_depth_with_an_all_subdirectories_default():
    from integrations.definition import DIRECTORY_DEPTHS
    from providers.general_rsync.definition import GeneralRsyncOptions, build
    from providers.general_webdav.definition import GeneralWebdavOptions
    assert GeneralRsyncOptions().directory_depth == "all"
    assert build(GeneralRsyncOptions(), None).depth == DiscoveryDepth.UNLIMITED
    assert {value: build(GeneralRsyncOptions(directory_depth=value), None).depth for value in DIRECTORY_DEPTHS} == \
        DIRECTORY_DEPTHS
    # One operator vocabulary for every source that has the setting.
    assert GeneralRsyncOptions.model_fields["directory_depth"].annotation == \
        GeneralWebdavOptions.model_fields["directory_depth"].annotation
    with pytest.raises(ValueError):
        GeneralRsyncOptions(directory_depth="4")


def test_the_executor_transport_schema_gains_no_discovery_setting():
    from executors.rsync.definition import RsyncOptions
    assert set(RsyncOptions.model_fields) == {"partial_transfers", "compression", "preserve_modification_time",
                                              "connection_timeout_seconds", "transfer_timeout_seconds"}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["rsync://h.example/pub/tree", "rsync+ssh://h.example/srv/tree"])
async def test_the_provider_issues_its_configured_depth_on_both_readings(url):
    from providers.general_rsync.definition import GeneralRsyncOptions, build
    provider = build(GeneralRsyncOptions(directory_depth="2"), None)
    result = await provider.resolve(TransferRequest(url.split(":", 1)[0], url))
    assert result.discovery.depth == DiscoveryDepth.of(2)
    if result.discovery.alternate is not None:
        # The SSH reading of the same request carries the same neutral depth.
        assert (await provider.resolve(result.discovery.alternate)).discovery.depth == DiscoveryDepth.of(2)


@pytest.mark.asyncio
async def test_default_rsync_behavior_is_unchanged(tmp_path, daemon, spawned):  # noqa: F811
    """The minimum regression: an installation that never touches the setting
    asks for, and lists, the whole tree with exactly the invocation it had."""
    from providers.general_rsync.definition import GeneralRsyncOptions, build
    origin, guard = daemon
    os.chmod(tmp_path / "srv" / "pub" / "tree" / "l1" / "l2" / "l3" / "locked", 0o755)
    provider = build(GeneralRsyncOptions(), None)
    request = await provider.resolve(TransferRequest("rsync", origin.url("/pub/tree")))
    assert request.discovery.depth == DiscoveryDepth.UNLIMITED and request.discovery.limits == DiscoveryLimits()
    executor = _executor(tmp_path, guard)
    tree = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/pub/tree"))),
                                   depth=request.discovery.depth)
    assert _paths(tree) == set(FILES) | {"l1/l2/l3/locked/never.bin"}
    recursive = [argv for argv in _listings(spawned) if "-r" in argv]
    assert recursive and not any(item.startswith("--exclude") or item.startswith("--filter")
                                 for argv in recursive for item in argv)


@pytest.mark.asyncio
async def test_directory_depth_persists_through_the_general_rsync_integration_scope():
    """The operator's choice is written to ``integrations.general_rsync`` by the
    one scoped configuration route; the executor's namespace is untouched."""
    from unittest.mock import patch

    from api import routes
    from core.config import AppSettings
    from executors.rsync.definition import definition as rsync_executor
    from integrations.definition import IntegrationSettings
    from providers.general_rsync.definition import definition as general_rsync
    from test_settings_namespace_mutation import _application

    current = AppSettings(integrations={
        "general_rsync": IntegrationSettings(options={}),
        "rsync": IntegrationSettings(options={"compression": True}),
    })
    application = _application(current)
    application.definitions = (general_rsync, rsync_executor)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings"):
        result = await routes.patch_integration_configuration(
            "general_rsync", routes.IntegrationConfigurationUpdate(options={"directory_depth": "1"}),
            application=application)
        assert result["options"]["directory_depth"] == "1"
        assert saved["cfg"].integrations["general_rsync"].options["directory_depth"] == "1"
        executor_options = saved["cfg"].integrations["rsync"].options
        assert executor_options["compression"] is True and "directory_depth" not in executor_options
        with pytest.raises(routes.HTTPException) as refused:
            await routes.patch_integration_configuration(
                "general_rsync", routes.IntegrationConfigurationUpdate(options={"directory_depth": "4"}),
                application=application)
        assert refused.value.status_code == 400
