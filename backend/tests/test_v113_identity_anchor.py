"""Durable torrent identity anchors across provider generations (#593).

An established torrent selection keeps its identity authority across
successive provider replacements even where an intermediate provider reports
no native torrent hash:

* Case A -- an attested generation precedes a fingerprint-less one: a later
  replacement is proven against the nearest attested generation of the root's
  own proven lineage (``TransferRepository._identity_anchor``), in the approved
  order (exact path, the strict basename/size bijection, the bounded
  coordinate correspondence). A fingerprint-less generation is crossed only
  when its COMPLETE list corresponds to the attested tree: a selection carried
  by exact path while unselected members differ is continuity, not identity.
* Case B -- a fingerprint-less generation comes first: a later replacement
  whose own provider attests the root's hash is bridged to it only by a
  complete correspondence of both file lists; once committed it is the anchor
  for every later switch.

Every chain commits real generations through the repository owner, with
neutral local providers; the #593 coordinates are its own shapes.
"""
from __future__ import annotations

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_root_provider_switch import (
    FLAT,
    MAGNET,
    SEASONS,
    SOURCE,
    first_conflict,
    first_hold,
    magnet_provider,
    members_of,
    offer_source,
    root_of,
    rows,
    settle,
)

from db import database
from fake_integrations import MemoryExecutor
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Permanence
from transfers.manual_route_switch import switch_root_provider
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

ROOT_HASH = SOURCE                                   # MAGNET's own btih
WRAPPED = [(name, f"Show/{path}", size) for name, path, size in SEASONS]
CHOSEN = ("S1/A.mkv", "S3/C.mkv")
CARRIED = ["S1/A.mkv", "S3/C.mkv"]
IDENTITIES = ("parcel-a", "parcel-b", "parcel-c", "parcel-d")


class Chain:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    async def start(self, monkeypatch, files, fp, *, fingerprint=ROOT_HASH, settle_by="confirm", everything=False):
        monkeypatch.setattr(database, "DB_PATH", self.tmp_path / "anchor.sqlite3")
        await database.init_db()
        self.registry = IntegrationRegistry()
        self.providers = {identity: magnet_provider(identity) for identity in IDENTITIES}
        for provider in self.providers.values():
            self.registry.register_provider(provider)
        self.clock = Clock()
        await self.boot()
        offer_source(self.providers["parcel-a"], files, fp)
        self.transfer = await self.engine.submit((TransferRequest(
            "magnet", MAGNET, name="Show", fingerprint=fingerprint, selection_mode="interactive"),),
            name="Show", deduplicate=False)
        for _ in range(3):
            await self.engine.tick()
        view = await self.repository.file_selection_presentation(self.transfer.id, now=self.engine.clock())
        if settle_by == "confirm":
            await self.repository.confirm_file_selection(self.transfer.id, view["manifest_id"], [
                entry["entry_id"] for entry in view["entries"]
                if everything or entry["relative_path"].rsplit("/", 2)[-2:] in (
                    [path.split("/")[0], path.split("/")[1]] for path in CHOSEN)], now=self.engine.clock())
        else:
            await self.repository.dismiss_file_selection(self.transfer.id, view["manifest_id"],
                                                         now=self.engine.clock())
        await settle(self.engine, 4)
        self.current = "parcel-a"
        return self

    async def boot(self):
        """A fresh repository and engine over the same durable database: the
        process restart every proof must survive."""
        self.repository = TransferRepository()
        self.registry.executors.clear()                                   # the restarted process's own executor
        self.registry.register_executor(MemoryExecutor(self.repository.authorize_execution))
        self.engine = TransferEngine(self.repository, self.registry, download_root=str(self.tmp_path / "dl"),
                                     policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=self.clock)
        await self.engine.initialize()

    async def hop(self, identity, files, fp, *, outcome="settle"):
        offer_source(self.providers[identity], files, fp)
        await switch_root_provider(self.engine, self.transfer.id, identity, expected_provider_id=self.current)
        self.current = identity
        if outcome == "conflict":
            return await first_conflict(self.repository, self.engine, self.transfer.id)
        if outcome == "held":
            return await first_hold(self.engine, self.transfer.id, identity)
        await settle(self.engine)
        return None

    async def generation(self, identity):
        (row,) = await rows("SELECT * FROM transfer_file_selections WHERE transfer_id=? AND provider_id=?",
                            (self.transfer.id, identity))
        return row

    async def carried(self, identity):
        generation = await self.generation(identity)
        assert generation["manifest_committed_at"] is not None and generation["continuity"] == "proven"
        root = await root_of(self.repository, self.transfer.id)
        assert root.resource.provider_id == identity and root.error is None
        return {path: payload for path, payload in await members_of(self.repository, self.transfer.id)}


async def targets(transfer_id):
    return sorted(row["local_path"].split("/dl/", 1)[1] for row in await rows(
        "SELECT local_path FROM download_files WHERE torrent_id=?", (transfer_id,)))


# -- Case A: an attested generation, a fingerprint-less one, then another provider -------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("label, files, fp, expected", [
    ("torbox-again", SEASONS, ROOT_HASH, {"S1/A.mkv": "x:S1/A.mkv", "S3/C.mkv": "x:S3/C.mkv"}),
    ("alldebrid-reordered", list(reversed(SEASONS)), ROOT_HASH, {"S1/A.mkv": "x:S1/A.mkv", "S3/C.mkv": "x:S3/C.mkv"}),
    ("debridlink-flat", FLAT, ROOT_HASH, {"S1/A.mkv": "x:A.mkv", "S3/C.mkv": "x:C.mkv"}),
    ("fingerprintless-again", [(n, f"Other/{p}", s) for n, p, s in SEASONS], "",
     {"S1/A.mkv": "x:Other/S1/A.mkv", "S3/C.mkv": "x:Other/S3/C.mkv"}),
], ids=lambda value: value if isinstance(value, str) else None)
async def test_an_attested_anchor_survives_a_fingerprintless_intermediate(tmp_path, monkeypatch, label, files, fp,
                                                                          expected):
    """#593: TorBox (attested) -> Premiumize (no hash, proven) -> the next
    provider is proven against the TorBox anchor -- exactly by path,
    by the strict basename bijection when it is flat, or by the bounded
    correspondence when it, too, reports no hash."""
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    before = await targets(chain.transfer.id)
    await chain.hop("parcel-b", WRAPPED, "")
    assert (await chain.carried("parcel-b")) == {"S1/A.mkv": "x:Show/S1/A.mkv", "S3/C.mkv": "x:Show/S3/C.mkv"}
    await chain.hop("parcel-c", files, fp)
    assert await chain.carried("parcel-c") == expected                       # N selected stay exactly N
    assert sorted(expected) == CARRIED
    assert await targets(chain.transfer.id) == before                       # destinations never rewritten
    assert len(await rows("SELECT 1 FROM download_files WHERE torrent_id=?", (chain.transfer.id,))) == len(before)
    import json
    for identity, reported in (("parcel-b", ""), ("parcel-c", fp)):          # no fingerprint fabricated
        for row in await rows("SELECT result FROM resolution_attempts WHERE provider_id=? AND result IS NOT NULL",
                              (identity,)):
            observation = (json.loads(row["result"]) or {}).get("observation") or {}
            assert observation.get("fingerprint", "") in {"", reported}


@pytest.mark.asyncio
async def test_the_anchor_proof_survives_a_restart(tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    await chain.boot()                                                      # nothing held in memory
    await chain.hop("parcel-c", FLAT, ROOT_HASH)
    assert await chain.carried("parcel-c") == {"S1/A.mkv": "x:A.mkv", "S3/C.mkv": "x:C.mkv"}


@pytest.mark.asyncio
async def test_a_settled_all_crosses_the_same_chain(tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH, settle_by="close")
    await chain.hop("parcel-b", WRAPPED, "")
    await chain.hop("parcel-c", FLAT, ROOT_HASH)
    assert await chain.carried("parcel-c") == {"S1/A.mkv": "x:A.mkv", "S2/B.mkv": "x:B.mkv", "S3/C.mkv": "x:C.mkv"}


# -- Case B: a fingerprint-less first generation, then an attesting provider ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("files, fp, expected", [
    (FLAT, ROOT_HASH, {"Show/S1/A.mkv": "x:A.mkv", "Show/S3/C.mkv": "x:C.mkv"}),          # strict bijection
    (WRAPPED, "", {"Show/S1/A.mkv": "x:Show/S1/A.mkv", "Show/S3/C.mkv": "x:Show/S3/C.mkv"}),   # fingerprint-less
], ids=["then-flat", "then-fingerprintless"])
async def test_a_fingerprintless_first_generation_is_bridged_by_a_later_attesting_provider(tmp_path, monkeypatch,
                                                                                         files, fp, expected):
    """Premiumize first; TorBox later attests the root's hash and its complete
    list corresponds to the established one by exactly one collection
    directory: the selection carries, and TorBox anchors the next switch."""
    chain = await Chain(tmp_path).start(monkeypatch, WRAPPED, "")
    before = await targets(chain.transfer.id)
    assert sorted(path for path, _ in await members_of(chain.repository, chain.transfer.id)) == [
        "Show/S1/A.mkv", "Show/S3/C.mkv"]                                    # the first provider's own coordinates
    await chain.hop("parcel-b", SEASONS, ROOT_HASH)
    assert await chain.carried("parcel-b") == {"Show/S1/A.mkv": "x:S1/A.mkv", "Show/S3/C.mkv": "x:S3/C.mkv"}
    await chain.hop("parcel-c", files, fp)
    assert await chain.carried("parcel-c") == expected
    assert await targets(chain.transfer.id) == before


@pytest.mark.asyncio
async def test_a_flattened_attested_list_cannot_vouch_for_a_later_fingerprintless_wrapped_one(tmp_path, monkeypatch):
    """A flat list keeps no directories, so a later fingerprint-less wrapped
    list has no approved proof against it (the strict bijection needs both
    hashes; the bounded correspondence allows one directory): refused."""
    chain = await Chain(tmp_path).start(monkeypatch, WRAPPED, "")
    await chain.hop("parcel-b", SEASONS, ROOT_HASH)
    await chain.hop("parcel-c", FLAT, ROOT_HASH)
    error = await chain.hop("parcel-d", WRAPPED, "", outcome="conflict")
    assert error is not None and error.diagnostic == "coordinate_no_correspondence"


# -- refusals -----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("first_fp, files, fp, fingerprint, expected", [
    ("", WRAPPED, "", ROOT_HASH, "fallback_missing_fingerprint"),          # nobody ever attests anything
    (ROOT_HASH, SEASONS, ROOT_HASH, "b" * 40, "fallback_missing_fingerprint"),   # root hash not its payload's
    # With an anchor, the reason is the last approved proof attempted (here the
    # bounded correspondence against the immediate predecessor).
    (ROOT_HASH, [(n, f"Other/{p}", s + 1) for n, p, s in SEASONS], ROOT_HASH, ROOT_HASH,
     "coordinate_no_correspondence"),                                       # same names, other sizes
    (ROOT_HASH, [(n, f"Other/{p}", s) for n, p, s in SEASONS[:2]], ROOT_HASH, ROOT_HASH,
     "coordinate_no_correspondence"),                                       # partial list
    (ROOT_HASH, [*[(n, f"Other/{p}", s) for n, p, s in SEASONS], ("D.mkv", "Other/S4/D.mkv", 400)], ROOT_HASH,
     ROOT_HASH, "coordinate_no_correspondence"),                            # extra member
    (ROOT_HASH, [(n, p.upper(), s) for n, p, s in SEASONS], ROOT_HASH, ROOT_HASH, "coordinate_no_correspondence"),
    (ROOT_HASH, [("A.mkv", "A.mkv", 100), ("B.mkv", "x/A.mkv", 100), ("C.mkv", "C.mkv", 300)], ROOT_HASH, ROOT_HASH,
     "coordinate_no_correspondence"),                                       # ambiguous by basename
], ids=["no-attestation", "unvalidated-root", "sizes", "partial", "extra", "case", "ambiguous"])
async def test_without_sufficient_evidence_the_chain_refuses_and_nothing_moves(tmp_path, monkeypatch, first_fp, files,
                                                                               fp, fingerprint, expected):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, first_fp, fingerprint=fingerprint)
    await chain.hop("parcel-b", WRAPPED, "")
    committed_b = (await chain.generation("parcel-b"))["manifest_committed_at"]
    if committed_b is None:                                                 # nothing bound the first hop either
        return
    members = await members_of(chain.repository, chain.transfer.id)
    error = await chain.hop("parcel-c", files, fp, outcome="conflict")
    assert error is not None and error.category == Category.RESOURCE_STATE_CONFLICT
    assert error.diagnostic == expected and error.permanence == Permanence.UNKNOWN
    assert (await chain.generation("parcel-c"))["manifest_committed_at"] is None
    assert await members_of(chain.repository, chain.transfer.id) == members


@pytest.mark.asyncio
async def test_a_replacement_reporting_another_hash_is_a_contradiction_on_the_operator_s_route(tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    held = await chain.hop("parcel-c", SEASONS, "c" * 40, outcome="held")
    assert held == "fallback_fingerprint_mismatch"


@pytest.mark.asyncio
async def test_a_later_fingerprintless_provider_never_bridges_a_fingerprintless_first_one(tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, WRAPPED, "")
    error = await chain.hop("parcel-b", SEASONS, "", outcome="conflict")
    assert error is not None and error.diagnostic == "fallback_missing_fingerprint"


@pytest.mark.asyncio
async def test_a_first_attesting_provider_after_a_fingerprintless_one_bridges_only_by_correspondence(
        tmp_path, monkeypatch):
    """A later hash never retroactively attests the earlier files: a flat
    attesting provider cannot be bridged by the coordinate proof, and the
    strict bijection needs both sides' own hashes -- so it refuses."""
    chain = await Chain(tmp_path).start(monkeypatch, WRAPPED, "")
    error = await chain.hop("parcel-b", FLAT, ROOT_HASH, outcome="conflict")
    assert error is not None and error.diagnostic == "coordinate_no_correspondence"


# -- the lineage rule itself, on a real committed chain -------------------------------------------------------------

async def _anchor(chain):
    """``_identity_anchor`` from the immediate predecessor the third hop would
    use, read in one session of the real repository."""
    predecessor = await chain.generation("parcel-b")
    root = await root_of(chain.repository, chain.transfer.id)
    async with database.get_db() as db:
        anchor = await chain.repository._identity_anchor(db, root, predecessor, ROOT_HASH)
    return anchor and anchor["provider_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("break_it", [
    "UPDATE transfer_file_selections SET continuity='held' WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET manifest_committed_at=NULL WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET decision_reason='confirmed' WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET continuity_reason='legacy_compatibility_reconstruction' "
    "WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET predecessor_id=NULL WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET predecessor_id='missing' WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET predecessor_id=id WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET continuity='held' WHERE provider_id='parcel-a'",
    "DELETE FROM resolution_attempts WHERE provider_id='parcel-b'",
], ids=["held", "uncommitted", "not-inherited", "legacy-reconstruction", "no-predecessor", "missing-predecessor",
        "cycle", "anchor-not-proven", "not-produced-for-root"])
async def test_an_untrustworthy_lineage_edge_anchors_nothing(tmp_path, monkeypatch, break_it):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    assert await _anchor(chain) == "parcel-a"                               # the intact chain anchors
    async with database.get_db() as db:
        await db.execute("PRAGMA foreign_keys=OFF")                       # a broken link the schema would refuse
        await db.execute(break_it)
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")
    assert await _anchor(chain) is None


@pytest.mark.asyncio
async def test_an_ancestor_attesting_another_hash_anchors_nothing(tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    predecessor = await chain.generation("parcel-b")
    root = await root_of(chain.repository, chain.transfer.id)
    async with database.get_db() as db:
        assert await chain.repository._identity_anchor(db, root, predecessor, "d" * 40) is None


@pytest.mark.asyncio
async def test_a_legacy_selection_without_an_intent_never_walks_the_lineage(tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    calls = []
    original = chain.repository._identity_anchor

    async def counted(*args, **kwargs):
        calls.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(chain.repository, "_identity_anchor", counted)
    async with database.get_db() as db:
        await db.execute("DELETE FROM transfer_file_selection_intent_entries")
        await db.execute("DELETE FROM transfer_file_selection_intents")
        await db.commit()
    await chain.hop("parcel-c", FLAT, ROOT_HASH, outcome="conflict")
    assert calls == []


# -- #593's own shapes, all 133 members -----------------------------------------------------------------------------

def _593():
    import json
    from pathlib import Path
    members = json.loads((Path(__file__).parent / "fixtures" / "transfer_593_members.json").read_text())["members"]
    return [(path.rsplit("/", 1)[-1], path, size) for path, size in members]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["alldebrid-or-torbox", "debridlink-flat"])
async def test_transfer_593_continues_after_premiumize_with_every_member(tmp_path, monkeypatch, shape):
    """The live chain: TorBox (attested, all 133 confirmed) -> Premiumize (the
    torrent's own folder above every member, no hash) -> AllDebrid/TorBox
    (identical paths) or Debrid-Link (flat), each attesting the root hash.
    On the shipped baseline every one refused with fallback_missing_fingerprint."""
    members = _593()
    premiumize = [(name, f"The Real Ghostbusters/{path}", size) for name, path, size in members]
    flat = [(name, name, size) for name, _path, size in members]
    chain = await Chain(tmp_path).start(monkeypatch, members, ROOT_HASH, everything=True)
    before = await targets(chain.transfer.id)
    assert len(before) == 133
    await chain.hop("parcel-b", premiumize, "")
    assert len(await chain.carried("parcel-b")) == 133
    await chain.hop("parcel-c", members if shape == "alldebrid-or-torbox" else flat, ROOT_HASH)
    carried = await chain.carried("parcel-c")
    assert sorted(carried) == sorted(path for _name, path, _size in members)
    expected = {path: f"x:{path if shape == 'alldebrid-or-torbox' else name}" for name, path, _size in members}
    assert carried == expected
    assert await targets(chain.transfer.id) == before


# -- selection continuity is not collection identity ----------------------------------------------------------------

EXTRA_A = ("a.nfo", "S9/a.nfo", 50)
EXTRA_B = ("b.nfo", "S9/b.nfo", 60)


def _with(files, extra, prefix=""):
    return [(name, f"{prefix}{path}", size) for name, path, size in [*files, extra]]


async def _truncate(provider_id):
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_file_manifests SET source_truncated=1 WHERE id IN "
                         "(SELECT manifest_id FROM transfer_file_selections WHERE provider_id=?)", (provider_id,))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("files, fp", [
    (_with(SEASONS, EXTRA_A, "Other/"), ""),                                 # a fingerprint-less next provider
    (_with(FLAT, ("a.nfo", "a.nfo", 50)), ROOT_HASH),                        # an attested flat next provider
], ids=["then-fingerprintless", "then-attested-flat"])
async def test_an_exact_path_selection_carry_is_no_identity_bridge(tmp_path, monkeypatch, files, fp):
    """TorBox attests MKVs + a.nfo; Premiumize lists the same MKVs + b.nfo.
    The selected MKVs carry onto Premiumize by exact path -- selection
    continuity -- but its complete list never corresponded to the attested
    one, so it bridges no identity to a later provider."""
    chain = await Chain(tmp_path).start(monkeypatch, _with(SEASONS, EXTRA_A), ROOT_HASH)
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_B), "")
    assert await chain.carried("parcel-b") == {"S1/A.mkv": "x:S1/A.mkv", "S3/C.mkv": "x:S3/C.mkv"}
    assert await _anchor(chain) is None
    members = await members_of(chain.repository, chain.transfer.id)
    error = await chain.hop("parcel-c", files, fp, outcome="conflict")
    assert error is not None and error.category == Category.RESOURCE_STATE_CONFLICT
    assert (await chain.generation("parcel-c"))["manifest_committed_at"] is None
    assert await members_of(chain.repository, chain.transfer.id) == members


@pytest.mark.asyncio
async def test_a_fingerprintless_first_tree_differing_from_the_attested_one_bridges_nothing(tmp_path, monkeypatch):
    """Premiumize first (MKVs + a.nfo); TorBox attests MKVs + b.nfo at the
    same paths, so the selection carries by exact path, and TorBox is an
    anchor by its own hash. A second fingerprint-less generation carried by
    exact path (MKVs + a.nfo again) never corresponded to TorBox's complete
    list: no later provider may borrow TorBox's authority through it."""
    chain = await Chain(tmp_path).start(monkeypatch, _with(SEASONS, EXTRA_A), "")
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_B), ROOT_HASH)
    assert await chain.carried("parcel-b") == {"S1/A.mkv": "x:S1/A.mkv", "S3/C.mkv": "x:S3/C.mkv"}
    await chain.hop("parcel-c", _with(SEASONS, EXTRA_A), "")
    assert await chain.carried("parcel-c") == {"S1/A.mkv": "x:S1/A.mkv", "S3/C.mkv": "x:S3/C.mkv"}
    root = await root_of(chain.repository, chain.transfer.id)
    async with database.get_db() as db:
        assert await chain.repository._identity_anchor(
            db, root, await chain.generation("parcel-c"), ROOT_HASH) is None
    error = await chain.hop("parcel-d", _with(SEASONS, EXTRA_A, "Other/"), "", outcome="conflict")
    assert error is not None and (await chain.generation("parcel-d"))["manifest_committed_at"] is None


@pytest.mark.asyncio
async def test_a_complete_fingerprintless_bridge_still_anchors_with_unselected_members(tmp_path, monkeypatch):
    """The same chain with Premiumize's complete list corresponding to
    TorBox's (unselected a.nfo included, one collection directory): it
    bridges, and a fingerprint-less next provider is proven against TorBox."""
    chain = await Chain(tmp_path).start(monkeypatch, _with(SEASONS, EXTRA_A), ROOT_HASH)
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_A, "Show/"), "")
    assert await _anchor(chain) == "parcel-a"
    await chain.boot()
    assert await _anchor(chain) == "parcel-a"                               # reconstructed from durable rows
    await chain.hop("parcel-c", _with(SEASONS, EXTRA_A, "Other/"), "")
    assert await chain.carried("parcel-c") == {"S1/A.mkv": "x:Other/S1/A.mkv", "S3/C.mkv": "x:Other/S3/C.mkv"}


@pytest.mark.asyncio
async def test_several_fingerprintless_generations_each_bridge_completely(tmp_path, monkeypatch):
    """TorBox -> Premiumize (Show/) -> another fingerprint-less (Other/) ->
    a third: every crossed generation's complete list corresponds to the
    anchor's, so the walk crosses both; one that does not ends it."""
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    await chain.hop("parcel-c", [(n, f"Other/{p}", s) for n, p, s in SEASONS], "")
    root = await root_of(chain.repository, chain.transfer.id)
    async with database.get_db() as db:
        anchor = await chain.repository._identity_anchor(db, root, await chain.generation("parcel-c"), ROOT_HASH)
    assert anchor["provider_id"] == "parcel-a"
    await chain.hop("parcel-d", FLAT, ROOT_HASH)
    assert await chain.carried("parcel-d") == {"S1/A.mkv": "x:A.mkv", "S3/C.mkv": "x:C.mkv"}


@pytest.mark.asyncio
@pytest.mark.parametrize("truncated", ["parcel-a", "parcel-b"], ids=["anchor", "crossed"])
async def test_a_truncated_manifest_on_the_lineage_anchors_nothing(tmp_path, monkeypatch, truncated):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    await _truncate(truncated)
    assert await _anchor(chain) is None


@pytest.mark.asyncio
async def test_a_truncated_fingerprintless_first_list_is_never_bridged(tmp_path, monkeypatch):
    """Case B demands the established list be COMPLETE: a source that may
    hold more than its recorded list is not bridged by a later hash."""
    chain = await Chain(tmp_path).start(monkeypatch, WRAPPED, "")
    await _truncate("parcel-a")
    error = await chain.hop("parcel-b", SEASONS, ROOT_HASH, outcome="conflict")
    assert error is not None and (await chain.generation("parcel-b"))["manifest_committed_at"] is None


# -- an attested generation vouches only for a lineage it completely corresponds to ---------------------------------

@pytest.mark.asyncio
async def test_an_attested_generation_reached_by_exact_path_from_a_fingerprintless_one_bridges_nothing(
        tmp_path, monkeypatch):
    """No-hash first (MKVs + a.nfo); TorBox attests MKVs + b.nfo at the same
    paths, so the selection carries by exact path -- but the two complete
    lists never corresponded. A no-hash provider listing TorBox's exact list
    under one more directory may not borrow TorBox's hash for a selection
    that was made on the other tree."""
    chain = await Chain(tmp_path).start(monkeypatch, _with(SEASONS, EXTRA_A), "")
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_B), ROOT_HASH)
    assert await chain.carried("parcel-b") == {"S1/A.mkv": "x:S1/A.mkv", "S3/C.mkv": "x:S3/C.mkv"}
    members = await members_of(chain.repository, chain.transfer.id)
    error = await chain.hop("parcel-c", _with(SEASONS, EXTRA_B, "Show/"), "", outcome="conflict")
    assert error is not None and error.category == Category.RESOURCE_STATE_CONFLICT
    assert error.permanence == Permanence.UNKNOWN                            # ordinary retry, never conclusive
    assert (await chain.generation("parcel-c"))["manifest_committed_at"] is None
    assert await members_of(chain.repository, chain.transfer.id) == members


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [_with(SEASONS, EXTRA_A), _with(SEASONS, EXTRA_A, "Show/")],
                         ids=["exact-path-identical-list", "one-directory-wrapper"])
async def test_an_attested_generation_completely_matching_the_fingerprintless_first_one_vouches(
        tmp_path, monkeypatch, first):
    """Positive controls: TorBox's complete list (unselected a.nfo included)
    corresponds to the no-hash first list -- identical, or one collection
    directory apart -- so TorBox vouches for the lineage, after a restart
    too, and the next no-hash provider is proven against it."""
    chain = await Chain(tmp_path).start(monkeypatch, first, "")
    established = sorted(path for path, _ in await members_of(chain.repository, chain.transfer.id))
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_A), ROOT_HASH)
    await chain.boot()                                                      # reconstructed from durable rows
    await chain.hop("parcel-c", _with(SEASONS, EXTRA_A, "Other/"), "")
    carried = await chain.carried("parcel-c")
    assert sorted(carried) == established
    assert sorted(carried.values()) == ["x:Other/S1/A.mkv", "x:Other/S3/C.mkv"]


@pytest.mark.asyncio
async def test_a_truncated_fingerprintless_first_list_gives_its_attested_successor_no_authority(
        tmp_path, monkeypatch):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, "")
    await chain.hop("parcel-b", SEASONS, ROOT_HASH)                         # exact path; lists identical
    await _truncate("parcel-a")                                             # but A's source held more
    error = await chain.hop("parcel-c", WRAPPED, "", outcome="conflict")
    assert error is not None and (await chain.generation("parcel-c"))["manifest_committed_at"] is None


@pytest.mark.asyncio
async def test_a_truncated_attested_list_is_no_complete_correspondence(tmp_path, monkeypatch):
    """The attested branch of the bounded correspondence: a recorded list
    whose source held more is not a complete list, whatever its provider
    reported."""
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await _truncate("parcel-a")
    error = await chain.hop("parcel-b", WRAPPED, "", outcome="conflict")
    assert error is not None and error.permanence == Permanence.UNKNOWN
    assert (await chain.generation("parcel-b"))["manifest_committed_at"] is None


# -- the strict bijection and the lineage's own origin --------------------------------------------------------------

FLAT_B = [*FLAT, ("b.nfo", "b.nfo", 60)]
FLAT_A = [*FLAT, ("a.nfo", "a.nfo", 50)]


@pytest.mark.asyncio
async def test_the_strict_bijection_carries_nothing_through_an_unproven_lineage(tmp_path, monkeypatch):
    """No-hash A (MKVs + a.nfo) -> attested B (MKVs + b.nfo, carried by exact
    path) -> attested C (B's files flattened). B and C report the root hash,
    so the basename/size bijection alone would carry C; but A and B never
    corresponded as collections, so C is refused, as an ordinary retry."""
    chain = await Chain(tmp_path).start(monkeypatch, _with(SEASONS, EXTRA_A), "")
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_B), ROOT_HASH)
    members = await members_of(chain.repository, chain.transfer.id)
    error = await chain.hop("parcel-c", FLAT_B, ROOT_HASH, outcome="conflict")
    assert error is not None and error.category == Category.RESOURCE_STATE_CONFLICT
    assert error.permanence == Permanence.UNKNOWN
    assert (await chain.generation("parcel-c"))["manifest_committed_at"] is None
    assert await members_of(chain.repository, chain.transfer.id) == members


@pytest.mark.asyncio
@pytest.mark.parametrize("first, first_fp, second", [
    (_with(SEASONS, EXTRA_A), ROOT_HASH, _with(SEASONS, EXTRA_B)),           # directly attested origin
    (_with(SEASONS, EXTRA_A), "", _with(SEASONS, EXTRA_A)),                  # no-hash origin, complete match
], ids=["attested-origin", "complete-fingerprintless-origin"])
async def test_the_strict_bijection_still_carries_a_proven_lineage(tmp_path, monkeypatch, first, first_fp, second):
    """Positive controls, across a restart: an attested origin vouches for
    its attested successor, and a no-hash origin whose complete list the
    attested successor matches does too -- the flat attested next provider
    is carried by the unchanged strict bijection."""
    chain = await Chain(tmp_path).start(monkeypatch, first, first_fp)
    await chain.hop("parcel-b", second, ROOT_HASH)
    await chain.boot()
    await chain.hop("parcel-c", FLAT_B if second == _with(SEASONS, EXTRA_B) else FLAT_A, ROOT_HASH)
    assert await chain.carried("parcel-c") == {"S1/A.mkv": "x:A.mkv", "S3/C.mkv": "x:C.mkv"}


async def _lineage(chain, identity="parcel-b"):
    root = await root_of(chain.repository, chain.transfer.id)
    async with database.get_db() as db:
        return await chain.repository._identity_lineage(db, root, await chain.generation(identity), ROOT_HASH)


@pytest.mark.asyncio
@pytest.mark.parametrize("break_it", [
    "UPDATE transfer_file_selections SET predecessor_id=NULL WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET predecessor_id='missing' WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET predecessor_id=id WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET request_id='foreign' WHERE provider_id='parcel-a'",
    "UPDATE transfer_file_selections SET continuity_reason='legacy_compatibility_reconstruction' "
    "WHERE provider_id='parcel-b'",
    "UPDATE transfer_file_selections SET manifest_committed_at=NULL WHERE provider_id='parcel-a'",
    "UPDATE transfer_file_selections SET continuity='held' WHERE provider_id='parcel-a'",
], ids=["inherited-without-predecessor", "missing-predecessor", "cycle", "foreign-predecessor",
        "legacy-reconstruction", "origin-uncommitted", "origin-held"])
async def test_an_inherited_generation_without_sound_provenance_vouches_for_nothing(tmp_path, monkeypatch, break_it):
    chain = await Chain(tmp_path).start(monkeypatch, SEASONS, ROOT_HASH)
    await chain.hop("parcel-b", WRAPPED, "")
    assert await _lineage(chain) is True                                    # the intact lineage
    await chain.boot()
    assert await _lineage(chain) is True                                    # reconstructed after a restart
    async with database.get_db() as db:
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute(break_it)
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")
    assert await _lineage(chain) is False


@pytest.mark.asyncio
async def test_an_attested_inherited_generation_that_lost_its_predecessor_bridges_nothing(tmp_path, monkeypatch):
    """The bootstrap bypass with B's provenance erased: an inherited
    generation naming no predecessor is no selection origin."""
    chain = await Chain(tmp_path).start(monkeypatch, _with(SEASONS, EXTRA_A), "")
    await chain.hop("parcel-b", _with(SEASONS, EXTRA_B), ROOT_HASH)
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_file_selections SET predecessor_id=NULL WHERE provider_id='parcel-b'")
        await db.commit()
    error = await chain.hop("parcel-c", _with(SEASONS, EXTRA_B, "Show/"), "", outcome="conflict")
    assert error is not None and (await chain.generation("parcel-c"))["manifest_committed_at"] is None
