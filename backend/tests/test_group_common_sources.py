"""Transfer-level common-source group switching — backend qualification.

MEMBERSHIP and ACTIONABILITY are independent facts and must stay that way:

* **common-source membership** — the raw whole-transfer canonical-host
  intersection across every current authoritative file's candidates,
  regardless of ``switch_eligible``, artifact operational state, or which
  candidate is currently selected. This is what "common source" means, and
  what ``common_candidate_count`` / launcher visibility is based on.
* **group actionability** — the subset of common hosts the group can
  currently converge to (every file's candidate for that host is already
  selected or switch-eligible). Controls only whether the chooser offers
  "Switch to this source"; never membership, count, or launcher visibility.
* **group ACTIVE** — the one common host every file is uniformly selected on.

Two producers must agree on MEMBERSHIP/count for the same transfer:

* the bounded Downloads / Dashboard-Recent list projection
  (``api.operational_downloads`` — ``common_candidate_count``), and
* the Details per-file candidate projection
  (``transfers.repository._candidate_presentation`` — ``source_candidates``),
  intersected the way the shared ``ui-group-candidates.js`` runtime does.

No new switching engine, candidate model, or lifecycle is introduced; the group
is a thin wrapper over the existing per-file canonical candidate sets.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

import api.operational_downloads as downloads
import db.database as database
from transfers.repository import TransferRepository


class _ExplodingRepository:
    async def presentation(self, *_args, **_kwargs):
        raise AssertionError(
            "The bounded group summary must never invoke comprehensive presentation per row"
        )


# ── Seeding ────────────────────────────────────────────────────────────────
#
# A scenario is a list of artifacts; each artifact is (status, [(host, candidate_id,
# is_selected), ...]). Hosts are single letters expanded to "<letter>.example".


def _host(letter: str) -> str:
    return f"{letter.lower()}.example"


async def _seed(db, name: str, artifacts, *, blocked_last: bool = False,
                standby_last: bool = False, extra_non_host_first: bool = False,
                no_bindings_last: bool = False):
    transfer_id = await db.execute_returning_id(
        "INSERT INTO torrents(hash, name, status) VALUES(?, ?, ?)",
        (f"grp-{name}-{uuid.uuid4().hex[:8]}", name, "downloading"),
    )
    for index, (status, candidates) in enumerate(artifacts):
        request_id = f"req-{name}-{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO transfer_requests(id, transfer_id, ordinal, payload) VALUES(?, ?, ?, ?)",
            (request_id, transfer_id, index, "{}"),
        )
        durable = [
            {"name": f"file-{index}.bin", "endpoints": [], "id": candidate_id}
            for (_h, candidate_id, _s) in candidates
        ]
        selected_index = next(
            (pos for pos, (_h, _c, is_selected) in enumerate(candidates) if is_selected),
            0,
        )
        blocked = 1 if (blocked_last and index == len(artifacts) - 1) else 0
        mirror_state = "standby" if (standby_last and index == len(artifacts) - 1) else ""
        artifact_id = await db.execute_returning_id(
            """INSERT INTO download_files
                (torrent_id, filename, status, request_id, candidates, selected_candidate, blocked, mirror_state)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (transfer_id, f"file-{index}.bin", status, request_id,
             json.dumps(durable), selected_index, blocked, mirror_state),
        )
        if no_bindings_last and index == len(artifacts) - 1:
            continue  # durable candidates only, no canonical binding rows
        order = 0
        if extra_non_host_first and index == 0:
            order += 1
            await db.execute(
                """INSERT INTO canonical_candidate_bindings
                    (canonical_artifact_id, candidate_id, provider_id, source_scope, source_key, role, candidate_order)
                    VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, f"scheme-{artifact_id}", "provider-a", "scheme", "HTTPS", "alternate", order),
            )
        for (letter, candidate_id, _is_selected) in candidates:
            order += 1
            await db.execute(
                """INSERT INTO canonical_candidate_bindings
                    (canonical_artifact_id, candidate_id, provider_id, source_scope, source_key, role, candidate_order)
                    VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (artifact_id, candidate_id, "provider-a", "host", _host(letter),
                 "canonical" if order == 1 else "alternate", order),
            )
    await db.commit()
    return transfer_id


@pytest_asyncio.fixture
async def group_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()

    @asynccontextmanager
    async def _open():
        async with database.get_db() as conn:
            yield conn

    yield _open


async def _list(application=None):
    return await downloads.list_operational_torrents(
        status=None, search=None, limit=25, offset=0,
        application=application or SimpleNamespace(repository=_ExplodingRepository(), definitions=[]),
    )


# ── Reference computation (mirrors ui-group-candidates.js computeGroup) ─────
#
# Deliberately mirrors the corrected JS: membership is the raw host
# intersection; actionability and ACTIVE are computed separately and never
# feed back into membership/count.


def reference_group(source_candidates_by_artifact: dict) -> dict:
    participants = list(source_candidates_by_artifact.values())
    if not participants:
        return {"common_hosts": [], "count": 0, "active_host": None, "actionable_hosts": []}

    common = None
    for entries in participants:
        hosts = {entry["source_host"] for entry in entries if entry.get("source_host")}
        common = hosts if common is None else (common & hosts)
    common_hosts = sorted(common or set())
    common_set = set(common_hosts)

    selected_hosts = [
        next((entry["source_host"] for entry in entries if entry.get("is_selected")), None)
        for entries in participants
    ]
    active_host = None
    if selected_hosts and all(host is not None and host == selected_hosts[0] for host in selected_hosts):
        if selected_hosts[0] in common_set:
            active_host = selected_hosts[0]

    actionable_hosts = [
        host for host in common_hosts
        if all(
            any(
                entry.get("source_host") == host and (entry.get("is_selected") or entry.get("switch_eligible"))
                for entry in entries
            )
            for entries in participants
        )
    ]

    return {
        "common_hosts": common_hosts, "count": len(common_hosts),
        "active_host": active_host, "actionable_hosts": actionable_hosts,
    }


async def _detail_group(transfer_id: int) -> dict:
    projection = await TransferRepository()._candidate_presentation(transfer_id)
    return reference_group({
        artifact_id: value["source_candidates"]
        for artifact_id, value in projection.items()
    })


# ── §12.1 / §12.4 / §13 — Raw membership ignores actionability ─────────────
# A host stays common (and counted) even when one unsatisfied current file
# cannot currently switch to it. This supersedes the previous (wrong)
# requirement that such a host be removed from the group.


@pytest.mark.asyncio
async def test_membership_persists_for_a_currently_non_switchable_host(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s121", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            # completed: B is neither selected nor switch-eligible on this file,
            # but A and B are both still canonical candidates of this file.
            ("completed", [("B", "b2", True), ("A", "a2", False)]),
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["count"] == 2
    # A is common but not actionable (the completed file can't switch to it);
    # B is common and actionable (the completed file is already selected there).
    assert detail["actionable_hosts"] == [_host("b")]
    item = next(row for row in (await _list())["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 2


# ── §19.1 Intersection truth (union vs. intersection) ──────────────────────


@pytest.mark.asyncio
async def test_intersection_truth_is_common_not_union(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s191", [
            ("downloading", [("A", "a1", True), ("B", "b1", False), ("C", "c1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False), ("D", "d2", False)]),
            ("downloading", [("A", "a3", True), ("B", "b3", False)]),
        ])
    result = await _list()
    item = next(row for row in result["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 2

    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["count"] == item["common_candidate_count"]


# ── §19.2 Zero intersection ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_intersection_yields_no_group(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s192", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("C", "c2", True), ("D", "d2", False)]),
        ])
    result = await _list()
    item = next(row for row in result["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 0
    assert (await _detail_group(transfer_id))["count"] == 0


# ── §19.3 / §12.7 Exactly one common host suppresses the launcher ──────────


@pytest.mark.asyncio
async def test_single_common_host_has_no_group_control(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s193", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("C", "c2", False)]),
        ])
    result = await _list()
    item = next(row for row in result["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 1
    assert (await _detail_group(transfer_id))["common_hosts"] == [_host("a")]


# ── §19.4 Two or more common hosts show the launcher ───────────────────────


@pytest.mark.asyncio
async def test_multiple_common_hosts_expose_launcher(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s194", [
            ("downloading", [("A", "a1", True), ("B", "b1", False), ("C", "c1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False), ("D", "d2", False)]),
        ])
    result = await _list()
    item = next(row for row in result["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 2


# ── §12.3 Actionability changes without changing count ─────────────────────


@pytest.mark.asyncio
async def test_actionability_toggle_does_not_change_membership_count(group_db):
    # Same canonical host sets both times; only F2's operational state (and
    # therefore B's switch-eligibility on F2) changes.
    async with group_db() as db:
        switchable_id = await _seed(db, "s123-on", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ])
        non_switchable_id = await _seed(db, "s123-off", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("completed", [("A", "a2", True), ("B", "b2", False)]),
        ])
    items = {row["id"]: row for row in (await _list())["items"]}
    assert items[switchable_id]["common_candidate_count"] == 2
    assert items[non_switchable_id]["common_candidate_count"] == 2

    on = await _detail_group(switchable_id)
    off = await _detail_group(non_switchable_id)
    assert on["common_hosts"] == off["common_hosts"] == [_host("a"), _host("b")]
    assert on["count"] == off["count"] == 2
    # Only actionability differs: B stops being an actionable convergence
    # target once F2 is completed, but it never leaves common_hosts.
    assert _host("b") in on["actionable_hosts"]
    assert _host("b") not in off["actionable_hosts"]


# ── §19.5 / §12.6 Uniform active source, independent of alternate actionability ──


@pytest.mark.asyncio
async def test_uniform_active_source_is_group_active(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s195", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["active_host"] == _host("a")
    assert detail["actionable_hosts"] == [_host("a"), _host("b")]
    item = next(row for row in (await _list())["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 2


@pytest.mark.asyncio
async def test_uniform_active_independent_of_alternate_actionability(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s126", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            # Also selected A, but completed -> cannot currently switch to B.
            ("completed", [("A", "a2", True), ("B", "b2", False)]),
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["count"] == 2
    assert detail["active_host"] == _host("a")
    # B remains common (and counted) but is not an actionable alternate.
    assert _host("b") not in detail["actionable_hosts"]


# ── §19.6 Mixed active sources — no group-active host ──────────────────────


@pytest.mark.asyncio
async def test_mixed_active_sources_have_no_group_active(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s196", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("B", "b2", True), ("A", "a2", False)]),
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["active_host"] is None
    item = next(row for row in (await _list())["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 2


# ── §12.5 Mixed state; one common target non-actionable ────────────────────


@pytest.mark.asyncio
async def test_mixed_state_one_common_target_non_actionable(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s125", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),   # can switch to B
            ("completed", [("B", "b2", True), ("A", "a2", False)]),    # cannot switch to A
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["active_host"] is None
    assert detail["actionable_hosts"] == [_host("b")]

    # Selecting B: skip the already-satisfied file, switch only the other.
    projection = await TransferRepository()._candidate_presentation(transfer_id)
    targets = {}
    for artifact_id, value in projection.items():
        for entry in value["source_candidates"]:
            if entry["source_host"] == _host("b"):
                targets[artifact_id] = entry
    already_on_b = [aid for aid, entry in targets.items() if entry["is_selected"]]
    needs_switch = [aid for aid, entry in targets.items() if entry["switch_eligible"]]
    assert len(already_on_b) == 1 and len(needs_switch) == 1


# ── §19.7 Partial current use of a target host ────────────────────────────


@pytest.mark.asyncio
async def test_partial_current_use_keeps_host_a_group_target(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s197", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
            ("downloading", [("B", "b3", True), ("A", "a3", False)]),
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["active_host"] is None
    assert detail["actionable_hosts"] == [_host("a"), _host("b")]
    projection = await TransferRepository()._candidate_presentation(transfer_id)
    # Selecting host A maps every not-yet-on-A file to its own candidate id.
    by_name = {}
    for artifact_id, value in projection.items():
        for entry in value["source_candidates"]:
            if entry["source_host"] == _host("a"):
                by_name[artifact_id] = entry
    # Two files already on A (selected), one on B and switch-eligible for A.
    selected = [aid for aid, entry in by_name.items() if entry["is_selected"]]
    switchable = [aid for aid, entry in by_name.items() if entry["switch_eligible"]]
    assert len(selected) == 2 and len(switchable) == 1


# ── §12.8 Two common sources, zero actionable targets ───────────────────────


@pytest.mark.asyncio
async def test_two_common_hosts_with_zero_actionable_targets_still_counts_and_shows(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s128", [
            ("completed", [("A", "a1", True), ("B", "b1", False)]),
            ("completed", [("B", "b2", True), ("A", "a2", False)]),
        ])
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a"), _host("b")]
    assert detail["count"] == 2
    assert detail["active_host"] is None
    assert detail["actionable_hosts"] == []
    item = next(row for row in (await _list())["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 2  # launcher still visible


# ── §19.9 / §12.9 Artifact-specific candidate ids per common host ──────────


@pytest.mark.asyncio
async def test_same_host_maps_to_per_file_candidate_ids(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "s199", [
            ("downloading", [("A", "aa1", True), ("B", "bb1", False)]),
            ("downloading", [("A", "aa7", True), ("B", "bb7", False)]),
        ])
    projection = await TransferRepository()._candidate_presentation(transfer_id)
    per_host = {}
    for artifact_id, value in projection.items():
        for entry in value["source_candidates"]:
            if entry["source_host"] == _host("a"):
                per_host[artifact_id] = entry["candidate_id"]
    assert sorted(per_host.values()) == ["aa1", "aa7"]


# ── §19.13 / §12.12 The normal list stays bounded and count-stable ─────────


@pytest.mark.asyncio
async def test_group_summary_keeps_the_list_bounded(group_db):
    async with group_db() as db:
        for index in range(8):
            await _seed(db, f"bounded-{index}", [
                ("downloading", [("A", f"a{index}", True), ("B", f"b{index}", False)]),
                ("downloading", [("A", f"a{index}b", True), ("B", f"b{index}b", False)]),
            ])

    calls = []
    real_get_db = database.get_db

    @asynccontextmanager
    async def counting():
        async with real_get_db() as conn:
            real_fetchall, real_fetchone = conn.fetchall, conn.fetchone

            async def fetchall(query, params=()):
                calls.append("fetchall")
                return await real_fetchall(query, params)

            async def fetchone(query, params=()):
                calls.append("fetchone")
                return await real_fetchone(query, params)

            conn.fetchall, conn.fetchone = fetchall, fetchone
            yield conn

    token = downloads.get_db
    downloads.get_db = counting
    try:
        result = await _list()
    finally:
        downloads.get_db = token

    assert calls == ["fetchall", "fetchone"]
    assert all(row["common_candidate_count"] == 2 for row in result["items"])
    assert len(result["items"]) == 8


@pytest.mark.asyncio
async def test_bounded_count_is_independent_of_state_eligibility_and_selection(group_db):
    """§12.12 — for identical canonical host sets, only a change in the host
    sets themselves may change ``common_candidate_count``; artifact state,
    switch_eligible, and which host is selected must not."""
    async with group_db() as db:
        variant_a = await _seed(db, "s1212-a", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ])
        variant_b = await _seed(db, "s1212-b", [
            ("completed", [("A", "a1", True), ("B", "b1", False)]),
            ("error", [("B", "b2", True), ("A", "a2", False)]),
        ])
        variant_c = await _seed(db, "s1212-c", [
            ("queued", [("B", "b1", True), ("A", "a1", False)]),
            ("paused", [("B", "b2", True), ("A", "a2", False)]),
        ])
    items = {row["id"]: row for row in (await _list())["items"]}
    assert items[variant_a]["common_candidate_count"] == 2
    assert items[variant_b]["common_candidate_count"] == 2
    assert items[variant_c]["common_candidate_count"] == 2


# ── Eligibility: blocked / standby artifacts never join the group ─────────
# (This is CURRENT-ARTIFACT identity, not switchability: a blocked/standby row
# is not one of the transfer's actual current files, so it must not
# participate in the intersection at all — same rule the Details candidate
# projection already applies.)


@pytest.mark.asyncio
async def test_blocked_and_standby_artifacts_are_excluded(group_db):
    async with group_db() as db:
        blocked_id = await _seed(db, "blocked", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("C", "c2", True), ("D", "d2", False)]),
        ], blocked_last=True)
        standby_id = await _seed(db, "standby", [
            ("downloading", [("A", "a3", True), ("B", "b3", False)]),
            ("downloading", [("C", "c4", True), ("D", "d4", False)]),
        ], standby_last=True)
    items = {row["id"]: row for row in (await _list())["items"]}
    # The excluded second artifact would have zeroed the intersection; with it
    # gone the sole remaining artifact's own hosts A/B are the common set.
    assert items[blocked_id]["common_candidate_count"] == 2
    assert items[standby_id]["common_candidate_count"] == 2
    assert (await _detail_group(blocked_id))["common_hosts"] == [_host("a"), _host("b")]


# ── Non-host-scoped candidates never seed a group host ─────────────────────


@pytest.mark.asyncio
async def test_non_host_scoped_candidate_does_not_seed_a_group_host(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "scheme", [
            # This file's ONLY host-scoped candidate is A (plus a non-host
            # scheme binding, which never carries a group host identity) — it
            # genuinely has no B candidate at all, regardless of its status.
            ("completed", [("A", "a1", True)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ], extra_non_host_first=True)
    detail = await _detail_group(transfer_id)
    assert detail["common_hosts"] == [_host("a")]
    item = next(row for row in (await _list())["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 1


# ── Unconsolidated member (no canonical bindings at all) ────────────────────


@pytest.mark.asyncio
async def test_member_without_canonical_bindings_has_no_known_hosts(group_db):
    async with group_db() as db:
        transfer_id = await _seed(db, "nobind", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
            ("downloading", [("A", "a3", True), ("B", "b3", False)]),
        ], no_bindings_last=True)
    # The third artifact has durable candidates but no canonical binding rows,
    # so its real host set is unknown/empty here — a file with no known
    # canonical host candidates makes the whole-transfer intersection empty
    # (§5: "If one current file has no canonical host-scoped candidate set,
    # the whole-transfer common-host intersection is empty"). This is a
    # membership fact about candidate PRESENCE, not an actionability gate.
    result = await _list()
    item = next(row for row in result["items"] if row["id"] == transfer_id)
    assert item["common_candidate_count"] == 0
    assert (await _detail_group(transfer_id))["count"] == 0


# ── Stale actionability vs. stale membership (§7, §12.10, §12.11) ──────────


@pytest.mark.asyncio
async def test_stale_actionability_leaves_membership_intact(group_db):
    """A host that stops being actionable is still common; membership does
    not change just because a file's operational state changed."""
    async with group_db() as db:
        before_id = await _seed(db, "stale-action-before", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ])
    before = await _detail_group(before_id)
    assert before["common_hosts"] == [_host("a"), _host("b")]
    assert _host("b") in before["actionable_hosts"]

    async with group_db() as db:
        after_id = await _seed(db, "stale-action-after", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("completed", [("A", "a2", True), ("B", "b2", False)]),  # B no longer actionable
        ])
    after = await _detail_group(after_id)
    assert after["common_hosts"] == [_host("a"), _host("b")]  # membership unchanged
    assert _host("b") not in after["actionable_hosts"]  # actionability changed


@pytest.mark.asyncio
async def test_stale_membership_drops_the_host_entirely(group_db):
    """A host that a file loses as a candidate altogether must leave the
    common set (and the count), independent of actionability."""
    async with group_db() as db:
        with_b_id = await _seed(db, "stale-member-with", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ])
        without_b_id = await _seed(db, "stale-member-without", [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True)]),  # B no longer a candidate at all
        ])
    with_b = await _detail_group(with_b_id)
    without_b = await _detail_group(without_b_id)
    assert _host("b") in with_b["common_hosts"]
    assert _host("b") not in without_b["common_hosts"]
    assert without_b["count"] == 1


# ── Cross-surface parity sweep ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_bounded_count_matches_detail_intersection_across_scenarios(group_db):
    scenarios = {
        "p-union": [
            ("downloading", [("A", "a1", True), ("B", "b1", False), ("C", "c1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False), ("D", "d2", False)]),
        ],
        "p-zero": [
            ("downloading", [("A", "a1", True)]),
            ("downloading", [("B", "b2", True)]),
        ],
        "p-one": [
            ("queued", [("A", "a1", True), ("B", "b1", False)]),
            ("paused", [("A", "a2", True), ("C", "c2", False)]),
        ],
        "p-mixed": [
            ("downloading", [("A", "a1", True), ("B", "b1", False)]),
            ("error", [("B", "b2", True), ("A", "a2", False)]),
        ],
        "p-completed": [
            ("completed", [("A", "a1", True), ("B", "b1", False)]),
            ("downloading", [("A", "a2", True), ("B", "b2", False)]),
        ],
    }
    ids = {}
    async with group_db() as db:
        for name, artifacts in scenarios.items():
            ids[name] = await _seed(db, name, artifacts)
    items = {row["id"]: row for row in (await _list())["items"]}
    for name, transfer_id in ids.items():
        detail = await _detail_group(transfer_id)
        assert items[transfer_id]["common_candidate_count"] == detail["count"], name

    # p-completed is the corrective case: a completed file's candidates still
    # count toward membership even though it cannot itself be switched, so the
    # common count is 2 (not 1, as an actionability-gated model would give).
    completed_detail = await _detail_group(ids["p-completed"])
    assert completed_detail["common_hosts"] == [_host("a"), _host("b")]
    assert items[ids["p-completed"]]["common_candidate_count"] == 2
    assert completed_detail["active_host"] == _host("a")
    # B stays common but is not actionable: the completed file can't reach it.
    assert completed_detail["actionable_hosts"] == [_host("a")]
