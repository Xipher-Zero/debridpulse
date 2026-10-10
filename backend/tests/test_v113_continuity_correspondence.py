"""Cross-provider continuation of an inherited torrent selection.

Phase 2 (D1): when exact paths cannot prove an inherited selection on a
replacement resource and the existing basename/size proof cannot either, a
distinct, bounded coordinate-interpretation proof
(``file_selection.coordinate_correspondence``) may carry it -- only when the
replacement is durably bound to the same torrent as the established tree
(``TransferRepository._same_torrent_correspondence``) and only through one
complete, one-to-one correspondence of BOTH whole file lists.

Phase 1 (D5): a refusal reached from durable evidence a later pass would read
unchanged is conclusive. On an automatic route it disqualifies the route
through the existing exhaustion/failover owner without charging the
provider; on an operator-chosen route it holds visibly; a replacement whose
list is still changing keeps the ordinary retry.

Every provider is a neutral local fixture; #590's Season 1 names and sizes
are real trace data (no credentials, no links).
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_live_provider_routing_corrective import native_refusal, refusing
from test_v113_root_provider_switch import (
    MAGNET,
    SEASONS,
    SOURCE,
    chosen_then_switched,
    magnet_provider,
    members_of,
    offer_source,
    root_of,
    rows,
    settle,
)

from db import database
from fake_integrations import MemoryExecutor
from transfers import file_selection as fs
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, Permanence, Retryability
from transfers.manual_route_switch import switch_root_provider
from transfers.models import SourceEntry, TransferRequest
from transfers.policy import TransferPolicy, continuity_disqualifying
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

# The root's own validated info-hash: MAGNET's btih.
ROOT_HASH = SOURCE
WRAPPED = [(name, f"Show/{path}", size) for name, path, size in SEASONS]

# #590, Season 1: the established (AllDebrid/TorBox/Real-Debrid) and the
# Premiumize coordinates of the same thirteen files, as the trace recorded them.
S590 = [
    ("The Real Ghostbusters 101 Ghosts 'R Us [MeR-DeR].avi", 182976512),
    ("The Real Ghostbusters 102 Killerwatt [MeR-DeR].avi", 183068672),
    ("The Real Ghostbusters 103 Mrs. Roger's Neighborhood [MeR-DeR].avi", 183109632),
    ("The Real Ghostbusters 104 Slimer, Come Home [MeR-DeR].avi", 183478272),
    ("The Real Ghostbusters 105 Troll Bridge [MeR-DeR].avi", 186359808),
    ("The Real Ghostbusters 106 The Boogieman Cometh [MeR-DeR].avi", 183164928),
    ("The Real Ghostbusters 107 Mr. Sandman, Dream Me a Dream [MeR-DeR].avi", 182898688),
    ("The Real Ghostbusters 108 When Halloween Was Forever [MeR-DeR].avi", 183064576),
    ("The Real Ghostbusters 109 Look Homeward, Ray [MeR-DeR].avi", 182966272),
    ("The Real Ghostbusters 110 Take Two [MeR-DeR].avi", 183427072),
    ("The Real Ghostbusters 111 Citizen Ghost [MeR-DeR].avi", 183042048),
    ("The Real Ghostbusters 112 Janine's Genie [MeR-DeR].avi", 183021568),
    ("The Real Ghostbusters 113 Xmas Marks the Spot [MeR-DeR].avi", 182990848),
]
ESTABLISHED_590 = [(f"The Real Ghostbusters S1/{name}", size) for name, size in S590]
PREMIUMIZE_590 = [(f"The Real Ghostbusters/The Real Ghostbusters S1/{name}", size) for name, size in S590]


def reason(established, replacement):
    with pytest.raises(fs.SelectionUnprovable) as refused:
        fs.coordinate_correspondence(established, replacement)
    return refused.value.reason


# -- Phase 2: the pure bounded proof --------------------------------------------------------------------------------

def test_590s_premiumize_list_corresponds_by_its_one_extra_collection_directory():
    proven = fs.coordinate_correspondence(ESTABLISHED_590, PREMIUMIZE_590)
    assert proven.interpretation == fs.CoordinateInterpretation.REPLACEMENT_WRAPPED
    assert proven.mapping == {path: f"The Real Ghostbusters/{path}" for path, _size in ESTABLISHED_590}


def test_the_established_side_may_carry_the_one_extra_directory_and_order_never_matters():
    proven = fs.coordinate_correspondence(list(reversed(PREMIUMIZE_590)), ESTABLISHED_590)
    assert proven.interpretation == fs.CoordinateInterpretation.ESTABLISHED_WRAPPED
    assert proven.mapping[PREMIUMIZE_590[0][0]] == ESTABLISHED_590[0][0]


def test_already_root_relative_lists_are_the_unchanged_interpretation():
    proven = fs.coordinate_correspondence(ESTABLISHED_590, list(reversed(ESTABLISHED_590)))
    assert proven.interpretation == fs.CoordinateInterpretation.UNCHANGED


@pytest.mark.parametrize("established, replacement", [
    # same names in distinct directories, same sizes: paths decide, uniquely
    ([("S1/e.mkv", 5), ("S2/e.mkv", 5)], [("Show/S1/e.mkv", 5), ("Show/S2/e.mkv", 5)]),
    # a folder named as the torrent inside the torrent is member hierarchy
    ([("Show/e.mkv", 5), ("x.nfo", 1)], [("Show/Show/e.mkv", 5), ("Show/x.nfo", 1)]),
    # a torrent whose only directory is Season 1/
    ([("Season 1/e1.mkv", 5), ("Season 1/e2.mkv", 6)], [("Show/Season 1/e1.mkv", 5), ("Show/Season 1/e2.mkv", 6)]),
    # directories on both sides
    ([("A/S1/e.mkv", 5), ("A/S2/f.mkv", 6)], [("S1/e.mkv", 5), ("S2/f.mkv", 6)]),
], ids=["same-names", "folder-named-as-torrent", "only-season-1", "both-sides"])
def test_permitted_layouts_have_exactly_one_correspondence(established, replacement):
    proven = fs.coordinate_correspondence(established, replacement)
    assert sorted(proven.mapping) == sorted(path for path, _size in established)
    assert sorted(proven.mapping.values()) == sorted(path for path, _size in replacement)


@pytest.mark.parametrize("established, replacement, expected", [
    (ESTABLISHED_590, PREMIUMIZE_590[:-1], "coordinate_no_correspondence"),                    # missing member
    (ESTABLISHED_590, PREMIUMIZE_590 + [("The Real Ghostbusters/extra.nfo", 9)], "coordinate_no_correspondence"),
    (ESTABLISHED_590[:5], PREMIUMIZE_590, "coordinate_no_correspondence"),                    # partial list
    ([("S1/e.mkv", 5), ("S1/e.mkv", 5)], [("Show/S1/e.mkv", 5)], "coordinate_established_duplicate_path"),
    ([("S1/e.mkv", 0)], [("Show/S1/e.mkv", 0)], "coordinate_established_unknown_size"),        # zero size
    ([("S1/e.mkv", None)], [("Show/S1/e.mkv", 5)], "coordinate_established_unknown_size"),     # unknown size
    ([("S1/e.mkv", 5)], [("Show/S1/E.mkv", 5)], "coordinate_no_correspondence"),               # case differs
    ([("S1/e.mkv", 5)], [("Show/S1/e.mkv", 6)], "coordinate_size_conflict"),
    ([("S1/e.mkv", 5)], [("../S1/e.mkv", 5)], "coordinate_replacement_unsafe_path"),          # malformed
    ([("S1/e.mkv", 5)], [("A/B/S1/e.mkv", 5)], "coordinate_no_correspondence"),               # two directories
    ([("S1/e.mkv", 5), ("S2/f.mkv", 6)], [("X/S1/e.mkv", 5), ("Y/S2/f.mkv", 6)], "coordinate_no_correspondence"),
    ([], PREMIUMIZE_590, "coordinate_established_manifest_missing"),
    # an unrelated torrent with similar names and sizes cannot match as a whole
    ([("Show/e1.mkv", 5), ("Show/e2.mkv", 6)], [("Other/e1.mkv", 5), ("Other/e3.mkv", 6)], "coordinate_no_correspondence"),
], ids=["missing", "extra", "partial", "duplicate", "zero-size", "unknown-size", "case", "size", "unsafe",
        "deeper-wrapper", "mixed-wrappers", "empty", "unrelated"])
def test_refused_layouts_fail_closed(established, replacement, expected):
    assert reason(established, replacement) == expected


def test_a_subset_selection_is_carried_only_after_the_whole_lists_correspond():
    proven = fs.coordinate_correspondence(ESTABLISHED_590, PREMIUMIZE_590)
    executable = tuple(SourceEntry(path.rsplit("/", 1)[-1], size, path,
                                   TransferRequest("https", f"https://pm.example/{index}"))
                       for index, (path, size) in enumerate(PREMIUMIZE_590))
    selected = [ESTABLISHED_590[0], ESTABLISHED_590[7]]
    migration = fs.migrate_by_correspondence(selected, ESTABLISHED_590, proven, executable,
                                             established=ESTABLISHED_590)
    assert [entry.relative_path for entry in migration.logical] == [path for path, _size in selected]
    assert [entry.relative_path for entry in migration.provenance] == [PREMIUMIZE_590[0][0], PREMIUMIZE_590[7][0]]
    assert [entry.request.payload for entry in migration.logical] == ["https://pm.example/0", "https://pm.example/7"]
    with pytest.raises(fs.SelectionUnprovable, match="coordinate_executable_path_missing"):
        fs.migrate_by_correspondence(selected, ESTABLISHED_590, proven, executable[1:], established=ESTABLISHED_590)


def test_continuity_disqualification_is_recognized_by_its_exact_facts_only():
    from transfers.errors import NormalizedError, Origin, Stage
    conclusive = NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION,
                                 Retryability.NEVER, permanence=Permanence.PERMANENT,
                                 diagnostic="fallback_missing_fingerprint")
    assert continuity_disqualifying(conclusive)
    assert not continuity_disqualifying(replace(conclusive, retryability=Retryability.UNKNOWN,
                                                permanence=Permanence.UNKNOWN))
    assert not continuity_disqualifying(replace(conclusive, origin=Origin.PROVIDER))
    assert not continuity_disqualifying(replace(conclusive, stage=Stage.RESOLUTION))


# -- Phase 2 through the commit boundary: the D1 same-torrent binding -----------------------------------------------

@pytest.mark.asyncio
async def test_a_trusted_fingerprintless_replacement_in_wrapped_coordinates_carries_the_selection(tmp_path, monkeypatch):
    """The root's info-hash is validated from its own payload, the predecessor's
    provider reported that hash, and the replacement was resolved for this very
    root: the whole lists correspond by one extra directory, so the selection is
    carried at its established paths with the replacement's own material."""
    repository, engine, transfer, _ = await chosen_then_switched(
        tmp_path, monkeypatch, target=WRAPPED, before=SOURCE, after="", fingerprint=ROOT_HASH)
    root = await root_of(repository, transfer.id)
    assert root.resource.provider_id == "parcel-b" and root.error is None
    (generation,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b'",
                               (transfer.id,))
    assert (generation["decision"], generation["decision_reason"], generation["continuity"]) == (
        "explicit", "inherited", "proven")
    # N selected stay N: the same logical paths, never broadened to S2/B.mkv.
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:Show/S1/A.mkv"),
                                                         ("S3/C.mkv", "x:Show/S3/C.mkv")]
    binding = await repository.resource_binding_id(transfer.id, root.resource.id)
    recorded = {row["entry_id"] for row in await rows(
        "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (generation["id"],))}
    assert recorded == {fs.entry_identity(binding, "Show/S1/A.mkv"), fs.entry_identity(binding, "Show/S3/C.mkv")}
    attempts = await rows("SELECT result FROM resolution_attempts WHERE provider_id='parcel-b'")
    assert all('"fingerprint":"' + ROOT_HASH not in (row["result"] or "") for row in attempts)   # none fabricated
    artifacts = await repository.artifacts(transfer.id)
    assert {candidate.provider_id for artifact in artifacts for candidate in artifact.candidates} == {"parcel-b"}
    assert any(artifact.execution is not None or artifact.state == "completed" for artifact in artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize("fingerprint, before, expected", [
    ("b" * 40, SOURCE, "fallback_missing_fingerprint"),              # recorded hash its payload does not reproduce
    (ROOT_HASH, "c" * 40, "fallback_missing_fingerprint"),           # established tree is another torrent's
    (ROOT_HASH, "", "fallback_missing_fingerprint"),                 # nothing binds the established tree
], ids=["unvalidated-root", "unrelated-predecessor", "unattested-predecessor"])
async def test_without_the_same_torrent_binding_the_strict_refusal_stands(tmp_path, monkeypatch, fingerprint, before,
                                                                          expected):
    """No binding: the strict proof's own refusal, and -- absent evidence
    being no contradiction -- the ordinary retried refusal, never permanent."""
    repository, _engine, transfer, error = await chosen_then_switched(
        tmp_path, monkeypatch, target=WRAPPED, before=before, after="", fingerprint=fingerprint, conflict=True)
    assert error is not None and error.diagnostic == expected
    assert error.permanence == Permanence.UNKNOWN and error.retryability == Retryability.UNKNOWN
    assert not await rows("SELECT 1 FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b' "
                          "AND manifest_committed_at IS NOT NULL", (transfer.id,))
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
async def test_a_replacement_reporting_another_torrent_is_a_conclusive_contradiction(tmp_path, monkeypatch):
    repository, _engine, transfer, held = await chosen_then_switched(
        tmp_path, monkeypatch, target=WRAPPED, before=SOURCE, after="b" * 40, fingerprint=ROOT_HASH, held=True)
    assert held == "fallback_fingerprint_mismatch"
    assert (await root_of(repository, transfer.id)).resource.provider_id == "parcel-b"      # the operator's route
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
async def test_a_trusted_replacement_whose_lists_do_not_correspond_is_refused_with_the_proof_s_reason(
        tmp_path, monkeypatch):
    """Bound to the same torrent, yet the lists do not correspond today: the
    list may still be completing, so the refusal is not permanent."""
    extra = [*WRAPPED, ("D.mkv", "Show/S4/D.mkv", 400)]
    repository, _engine, transfer, error = await chosen_then_switched(
        tmp_path, monkeypatch, target=extra, before=SOURCE, after="", fingerprint=ROOT_HASH, conflict=True)
    assert error is not None and error.diagnostic == "coordinate_no_correspondence"
    assert error.permanence == Permanence.UNKNOWN
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
async def test_exact_paths_still_prove_first_and_the_basename_proof_is_unchanged(tmp_path, monkeypatch):
    """A flattened replacement with both fingerprints is the existing basename
    proof's, exactly as before; nothing here reaches the new branch."""
    repository, _engine, transfer, _ = await chosen_then_switched(
        tmp_path, monkeypatch, fingerprint=ROOT_HASH)
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:A.mkv"), ("S3/C.mkv", "x:C.mkv")]


# -- Phase 1: automatic versus operator routes ----------------------------------------------------------------------

async def automatic_successor(tmp_path, monkeypatch, target, *, fingerprint="", after="", fallback=None,
                              mutate=None):
    """#590's opening: ``parcel-a`` decomposes an explicit selection, the
    operator switches to ``parcel-b``, which refuses the content after its
    manifest (Real-Debrid code 35), and routing moves AUTOMATICALLY to
    ``parcel-c`` (``parcel-a`` is disabled meanwhile). ``fallback`` offers
    ``parcel-d`` the given list; ``mutate`` lets ``parcel-c`` change its
    executable list after its observation was recorded."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "automatic.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    first, refuser = magnet_provider("parcel-a"), refusing(magnet_provider("parcel-b"),
                                                           native_refusal(35, "infringing_file"))
    successor, last = magnet_provider("parcel-c"), magnet_provider("parcel-d")
    for provider in (first, refuser, successor, *((last,) if fallback is not None else ())):
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, resolution_retry_delay=300.0, max_attempts=3),
                            clock=Clock())
    await engine.initialize()
    offer_source(first, SEASONS, SOURCE)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", fingerprint=fingerprint,
                                                    selection_mode="interactive"),), name="Show", deduplicate=False)
    for _ in range(3):
        await engine.tick()
    view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], [
        entry["entry_id"] for entry in view["entries"] if entry["relative_path"] in ("S1/A.mkv", "S3/C.mkv")],
        now=engine.clock())
    await settle(engine, 4)
    offer_source(refuser, SEASONS, SOURCE)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    first.descriptor = replace(first.descriptor, enabled=False)
    resource = offer_source(successor, target, after)
    if mutate:
        mutate(successor, resource)
    if fallback is not None:
        offer_source(last, fallback, SOURCE)
    await settle(engine, 8)
    return repository, engine, transfer


async def route_rows(request_id):
    return await rows("""SELECT a.provider_id,a.state,a.error,a.reentry_at,p.operation FROM route_attempt_provenance p
        JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE a.request_id=? ORDER BY p.ordinal""",
                      (request_id,))


@pytest.mark.asyncio
async def test_an_automatic_route_conclusively_unprovable_is_disqualified_and_failover_continues(tmp_path, monkeypatch):
    """Its own provider reports another torrent: a conclusive contradiction."""
    repository, _engine, transfer = await automatic_successor(tmp_path, monkeypatch, WRAPPED, after="b" * 40,
                                                              fallback=SEASONS)
    root = await root_of(repository, transfer.id)
    history = await route_rows(root.id)
    successor = [row for row in history if row["provider_id"] == "parcel-c"]
    assert [(row["state"], row["operation"]) for row in successor] == [("exhausted", "resolve")]
    assert '"diagnostic":"fallback_fingerprint_mismatch"' in successor[0]["error"]
    assert '"domain":"lifecycle"' in successor[0]["error"] and '"permanence":"permanent"' in successor[0]["error"]
    assert successor[0]["reentry_at"] is None                                  # no transient budget charged
    assert "parcel-c" not in await repository.provider_reentries(root.id)
    assert "parcel-c" in await repository.exhausted_route_providers(root.id)
    # The existing competition continued to parcel-d, whose exact paths prove.
    assert await repository.bound_route_provider(root.id) == "parcel-d"
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]
    assert (await rows("SELECT status FROM torrents WHERE id=?", (transfer.id,)))[0]["status"] != "failed"
    assert not await rows("SELECT 1 FROM application_events WHERE transfer_id=? AND kind='error'", (transfer.id,))


@pytest.mark.asyncio
async def test_with_no_safe_alternative_the_disqualified_request_fails_truthfully_once(tmp_path, monkeypatch):
    repository, _engine, transfer = await automatic_successor(tmp_path, monkeypatch, WRAPPED, after="b" * 40)
    root = await root_of(repository, transfer.id)
    assert root.state == "failed" and root.error.diagnostic == "fallback_fingerprint_mismatch"
    successor = [row for row in await route_rows(root.id) if row["provider_id"] == "parcel-c"]
    assert [row["state"] for row in successor] == ["exhausted"]               # one disqualification, no retries
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:S1/A.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
async def test_an_unchanged_but_unbound_list_on_an_automatic_route_is_never_declared_permanent(tmp_path, monkeypatch):
    """Finding A: the replacement's live list equals the list it recorded, yet
    nothing proves that list frozen and nothing contradicts the established
    torrent -- the fingerprint is merely absent. Not conclusive: the route is
    neither disqualified nor held, and the ordinary bounded retry remains."""
    repository, engine, transfer = await automatic_successor(tmp_path, monkeypatch, WRAPPED)
    root = await root_of(repository, transfer.id)
    assert await repository.bound_route_provider(root.id) == "parcel-c"
    assert "parcel-c" not in await repository.exhausted_route_providers(root.id)
    assert root.error is not None and root.error.diagnostic == "fallback_missing_fingerprint"
    assert root.error.permanence == Permanence.UNKNOWN and root.error.retryability == Retryability.UNKNOWN
    assert root.retry_at > engine.clock()


@pytest.mark.asyncio
async def test_an_automatic_trusted_replacement_is_carried_without_any_disqualification(tmp_path, monkeypatch):
    repository, _engine, transfer = await automatic_successor(tmp_path, monkeypatch, WRAPPED, fingerprint=ROOT_HASH)
    root = await root_of(repository, transfer.id)
    assert await repository.bound_route_provider(root.id) == "parcel-c"
    assert "parcel-c" not in await repository.exhausted_route_providers(root.id)
    assert await members_of(repository, transfer.id) == [("S1/A.mkv", "x:Show/S1/A.mkv"),
                                                         ("S3/C.mkv", "x:Show/S3/C.mkv")]


@pytest.mark.asyncio
async def test_a_replacement_still_changing_its_list_keeps_the_ordinary_retry(tmp_path, monkeypatch):
    """Its executable list is not the manifest it recorded: the refusal is not
    conclusive, so the route is neither disqualified nor held -- the ordinary
    bounded retry applies, exactly as before."""
    def changing(provider, resource):
        provider.members[resource.id] = provider.members[resource.id][:-1]

    repository, engine, transfer = await automatic_successor(tmp_path, monkeypatch, WRAPPED, mutate=changing)
    root = await root_of(repository, transfer.id)
    assert await repository.bound_route_provider(root.id) == "parcel-c"
    assert "parcel-c" not in await repository.exhausted_route_providers(root.id)
    assert root.error is not None and root.error.category == Category.RESOURCE_STATE_CONFLICT
    assert root.error.retryability == Retryability.UNKNOWN and root.error.permanence == Permanence.UNKNOWN
    assert root.retry_at > engine.clock()
    assert not await rows("SELECT 1 FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-c' "
                          "AND continuity='held'", (transfer.id,))


# -- Finding B: a settled ALL crosses the same boundary through the same proofs ------------------------------------

async def settled_then_switched(tmp_path, monkeypatch, *, target, after="", fingerprint=ROOT_HASH, settled_by="close",
                                conflict=False, mode="interactive", member_edit=False):
    """An interactive root on ``parcel-a`` whose window the operator settles to
    ALL -- by Close/X (``settled_by="close"``) or by letting the decision
    timeout run out (``"timeout"``) -- which records ALL as the transfer's concrete
    intent; then an operator switch to ``parcel-b``. A transfer holding an
    intent opens every later generation already carrying it (inherited), so
    no second window opens. ``mode="all"`` instead submits an ordinary ALL;
    ``member_edit`` then has the operator deselect and reselect one member
    after the replacement's generation opened -- the edit that gives the root
    its intent (every member, established coordinates) only then."""
    from test_v113_root_provider_switch import first_conflict

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "settled.sqlite3")
    await database.init_db()
    repository, registry = TransferRepository(), IntegrationRegistry()
    providers = {identity: magnet_provider(identity) for identity in ("parcel-a", "parcel-b")}
    for provider in providers.values():
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "dl"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=Clock())
    await engine.initialize()

    async def settle_window():
        for _ in range(3):
            await engine.tick()
        if mode == "all":
            return
        if settled_by == "close":
            view = await repository.file_selection_presentation(transfer.id, now=engine.clock())
            outcome = await repository.dismiss_file_selection(transfer.id, view["manifest_id"], now=engine.clock())
            assert outcome.decision == "all"
        else:
            for _ in range(3):
                engine.clock.now += 200                                   # past the decision window
                await engine.tick()

    offer_source(providers["parcel-a"], SEASONS, SOURCE)
    transfer = await engine.submit((TransferRequest("magnet", MAGNET, name="Show", fingerprint=fingerprint,
                                                    selection_mode=mode),), name="Show", deduplicate=False)
    await settle_window()
    await settle(engine, 4)
    offer_source(providers["parcel-b"], target, after)
    await switch_root_provider(engine, transfer.id, "parcel-b", expected_provider_id="parcel-a")
    if mode == "all" and member_edit:
        await settle(engine, 2)                            # the replacement's generation opens (decided ALL)
        (artifact,) = [item for item in await repository.artifacts(transfer.id)
                       if item.target.endswith("/S2/B.mkv")]
        await engine.select_artifact(transfer.id, artifact.id, selected=False)   # the operator's own edits:
        await engine.select_artifact(transfer.id, artifact.id, selected=True)    # the intent is born, all kept
    error = await first_conflict(repository, engine, transfer.id) if conflict else await settle(engine)
    return repository, engine, transfer, error


async def intent_of(transfer_id):
    return sorted((row["relative_path"], row["expected_bytes"]) for row in await rows(
        """SELECT e.relative_path,e.expected_bytes FROM transfer_file_selection_intent_entries e
           JOIN transfer_file_selection_intents i ON i.request_id=e.request_id WHERE i.transfer_id=?""", (transfer_id,)))


ALL_SEASONS = [("S1/A.mkv", 100), ("S2/B.mkv", 200), ("S3/C.mkv", 300)]


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", ["close", "timeout"])
async def test_a_settled_all_crosses_a_provable_wrapper_difference(tmp_path, monkeypatch, settled):
    repository, _engine, transfer, _ = await settled_then_switched(tmp_path, monkeypatch, target=WRAPPED,
                                                                   settled_by=settled)
    assert await intent_of(transfer.id) == ALL_SEASONS                    # the intent, unchanged
    (generation,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b'",
                               (transfer.id,))
    # Born carrying the settled intent: the inherited branch's proofs carry it.
    assert (generation["decision"], generation["decision_reason"], generation["continuity"]) == (
        "explicit", "inherited", "proven")
    assert generation["manifest_committed_at"] is not None
    # Every established member, at its established path, with the replacement's own material.
    assert await members_of(repository, transfer.id) == [
        ("S1/A.mkv", "x:Show/S1/A.mkv"), ("S2/B.mkv", "x:Show/S2/B.mkv"), ("S3/C.mkv", "x:Show/S3/C.mkv")]
    targets = sorted(row["local_path"].rsplit("/", 2)[-2] + "/" + row["local_path"].rsplit("/", 1)[-1]
                     for row in await rows("SELECT local_path FROM download_files WHERE torrent_id=?", (transfer.id,)))
    assert targets == ["S1/A.mkv", "S2/B.mkv", "S3/C.mkv"]                 # destinations never rewritten


@pytest.mark.asyncio
async def test_a_settled_all_with_exact_paths_is_carried_by_exact_path_first(tmp_path, monkeypatch):
    repository, _engine, transfer, _ = await settled_then_switched(tmp_path, monkeypatch, target=SEASONS,
                                                                   after=SOURCE)
    assert await members_of(repository, transfer.id) == [
        ("S1/A.mkv", "x:S1/A.mkv"), ("S2/B.mkv", "x:S2/B.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
@pytest.mark.parametrize("fingerprint, target, expected", [
    ("", WRAPPED, "fallback_missing_fingerprint"),                                   # no same-torrent binding
    (ROOT_HASH, [*WRAPPED, ("D.mkv", "Show/S4/D.mkv", 400)], "coordinate_no_correspondence"),   # not the same list
    (ROOT_HASH, WRAPPED[:2], "coordinate_no_correspondence"),                         # partial list
], ids=["unbound", "extra", "partial"])
async def test_a_settled_all_without_proof_is_refused_and_nothing_moves(tmp_path, monkeypatch, fingerprint, target,
                                                                        expected):
    repository, _engine, transfer, error = await settled_then_switched(
        tmp_path, monkeypatch, target=target, fingerprint=fingerprint, conflict=True)
    assert error is not None and error.diagnostic == expected and error.permanence == Permanence.UNKNOWN
    assert not await rows("SELECT 1 FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b' "
                          "AND manifest_committed_at IS NOT NULL", (transfer.id,))
    assert await members_of(repository, transfer.id) == [
        ("S1/A.mkv", "x:S1/A.mkv"), ("S2/B.mkv", "x:S2/B.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
async def test_an_intent_given_to_an_ordinary_all_root_later_has_no_complete_list_to_prove_and_stays_refused(
        tmp_path, monkeypatch):
    """The only route into a decided-ALL generation holding an intent from
    other coordinates: an ordinary-ALL root, a replacement whose generation
    opened decided ALL, then the operator's member edits give the root its
    intent. An ALL-at-creation generation never records its file list, so no
    complete established list exists for any whole-list proof: the exact-path
    refusal stands, and nothing commits or moves."""
    repository, _engine, transfer, error = await settled_then_switched(
        tmp_path, monkeypatch, target=WRAPPED, mode="all", member_edit=True, conflict=True)
    assert await intent_of(transfer.id) == ALL_SEASONS
    assert not await rows("SELECT 1 FROM transfer_file_selections WHERE provider_id='parcel-a' "
                          "AND manifest_id IS NOT NULL")                     # the predecessor recorded no list
    assert error is not None and error.diagnostic == "selected_path_missing"
    assert not await rows("SELECT 1 FROM transfer_file_selections WHERE transfer_id=? AND provider_id='parcel-b' "
                          "AND manifest_committed_at IS NOT NULL", (transfer.id,))
    assert await members_of(repository, transfer.id) == [
        ("S1/A.mkv", "x:S1/A.mkv"), ("S2/B.mkv", "x:S2/B.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]


@pytest.mark.asyncio
async def test_an_ordinary_initial_all_records_no_intent_and_inherits_nothing(tmp_path, monkeypatch):
    """ALL chosen at submission is never a selection: no intent is recorded,
    so no later generation inherits one, and its continuity stays the
    existing established-decomposition rule (which holds a different
    decomposition, exactly as before)."""
    repository, _engine, transfer, _ = await settled_then_switched(tmp_path, monkeypatch, target=WRAPPED, mode="all")
    assert await intent_of(transfer.id) == []
    held = await rows("SELECT continuity,continuity_reason FROM transfer_file_selections WHERE transfer_id=? "
                      "AND provider_id='parcel-b'", (transfer.id,))
    assert [row["continuity"] for row in held] == ["held"]
    assert await members_of(repository, transfer.id) == [
        ("S1/A.mkv", "x:S1/A.mkv"), ("S2/B.mkv", "x:S2/B.mkv"), ("S3/C.mkv", "x:S3/C.mkv")]
