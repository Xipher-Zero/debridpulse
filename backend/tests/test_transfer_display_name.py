"""Focused regressions for the canonical transfer display-name normalizer.

Covers DP 1.0.12 UI presentation task Section 27 cases O-T: the pure
``normalized_transfer_display_name`` helper is deterministic, conservative,
prefers artifact-derived names over the root/request name, and never exposes
raw source-host/hash bookkeeping when meaningful filenames exist.
"""
from transfers.display_name import normalized_transfer_display_name


# ── O: single file ──────────────────────────────────────────────────────


def test_single_file_uses_its_own_filename():
    assert normalized_transfer_display_name(["Example.mkv"]) == "Example.mkv"


# ── P: multipart archive normalization ──────────────────────────────────


def test_multipart_rar_set_normalizes_to_shared_base_plus_total_files():
    filenames = [f"Example.Release.part{index:02d}.rar" for index in range(1, 25)]
    assert (
        normalized_transfer_display_name(filenames)
        == "Example.Release + 24 files"
    )


def test_multivolume_r_suffix_set_normalizes_to_shared_base():
    filenames = ["Show.S01.r00", "Show.S01.r01", "Show.S01.r02"]
    assert normalized_transfer_display_name(filenames) == "Show.S01 + 3 files"


def test_numbered_7z_split_set_normalizes_to_shared_base():
    filenames = ["Archive.7z.001", "Archive.7z.002", "Archive.7z.003"]
    assert normalized_transfer_display_name(filenames) == "Archive + 3 files"


# ── Q: safe common-base normalization (case-insensitive base match) ─────


def test_common_base_match_is_case_insensitive():
    filenames = ["Movie.PART01.rar", "movie.part02.rar", "MOVIE.Part03.rar"]
    assert normalized_transfer_display_name(filenames) == "Movie + 3 files"


# ── R: unrelated filenames — no over-normalization ───────────────────────


def test_unrelated_filenames_do_not_invent_a_common_name():
    filenames = ["alpha.mkv", "beta.srt", "gamma.nfo"]
    assert normalized_transfer_display_name(filenames) == "alpha.mkv + 2 files"


def test_mixed_recognized_and_unrelated_bases_fall_back_to_representative_plus_count():
    # One file matches a multipart pattern, the other does not -> the bases
    # disagree, so this conservatively falls back rather than fabricating a
    # false common identity.
    filenames = ["Example.Release.part01.rar", "readme.txt"]
    assert (
        normalized_transfer_display_name(filenames)
        == "Example.Release.part01.rar + 1 files"
    )


# ── S: missing/empty filenames — fallback order ──────────────────────────


def test_no_filenames_falls_back_to_root_name():
    assert normalized_transfer_display_name([], root_name="My Transfer") == "My Transfer"


def test_blank_filenames_are_ignored_like_missing():
    assert normalized_transfer_display_name(["", "   ", None], root_name="Root") == "Root"


def test_no_filenames_and_no_root_name_falls_back_to_generic_unnamed():
    assert normalized_transfer_display_name([]) == "(unnamed)"
    assert normalized_transfer_display_name([], root_name="") == "(unnamed)"
    assert normalized_transfer_display_name([], root_name="   ") == "(unnamed)"


# ── T: source-host/raw-root regression ───────────────────────────────────


def test_artifact_derived_name_never_exposes_the_raw_root_source_name():
    root_name = "1fichier.com - abcdef123456 + 23 more"
    filenames = [f"Example.Release.part{index:02d}.rar" for index in range(1, 25)]
    result = normalized_transfer_display_name(filenames, root_name=root_name)
    assert result == "Example.Release + 24 files"
    assert "1fichier.com" not in result
    assert "abcdef123456" not in result


def test_single_meaningful_filename_wins_over_root_source_name():
    result = normalized_transfer_display_name(
        ["Movie.Title.2026.1080p.mkv"],
        root_name="1fichier.com - xo3nibyjy94ymn937127",
    )
    assert result == "Movie.Title.2026.1080p.mkv"


# ── Determinism / conservatism ────────────────────────────────────────────


def test_normalizer_is_pure_and_deterministic():
    filenames = ["Example.Release.part01.rar", "Example.Release.part02.rar"]
    first = normalized_transfer_display_name(filenames, root_name="root")
    second = normalized_transfer_display_name(list(filenames), root_name="root")
    assert first == second == "Example.Release + 2 files"


# ── DP 1.0.12 Workstream C: torrent/magnet root-name regression correction ─
# A torrent/magnet transfer's durable root/request name is its real identity
# and must win over any single member artifact filename — the defect this
# section closes (a 15-file torrent rendered as its 14th track's filename).


def test_torrent_submission_root_name_wins_over_member_filenames():
    filenames = [f"{index:02d}.mkv" for index in range(1, 16)]
    result = normalized_transfer_display_name(
        filenames, root_name="Example Torrent", root_is_canonical_identity=True,
    )
    assert result == "Example Torrent"


def test_magnet_with_meaningful_dn_root_name_wins_over_member_filenames():
    filenames = [f"track-{index:02d}.mp3" for index in range(1, 16)]
    result = normalized_transfer_display_name(
        filenames, root_name="Example Release", root_is_canonical_identity=True,
    )
    assert result == "Example Release"


def test_regression_example_nsync_essentials_root_title_not_replaced_by_member_file():
    root_name = "NSYNC - Essentials (2020) Mp3 320kbps [PMEDIA] ⭐"
    filenames = ["14. I'll Never Stop (Radio Edit).mp3"] + [
        f"{index:02d}. Track {index}.mp3" for index in range(1, 15)
    ]
    result = normalized_transfer_display_name(
        filenames, root_name=root_name, root_is_canonical_identity=True,
    )
    assert result == root_name


def test_torrent_root_name_wins_even_for_single_file_torrent():
    result = normalized_transfer_display_name(
        ["14. I'll Never Stop (Radio Edit).mp3"],
        root_name="Example Torrent",
        root_is_canonical_identity=True,
    )
    assert result == "Example Torrent"


def test_torrent_with_blank_root_name_falls_back_to_artifact_normalization():
    # root_is_canonical_identity is asserted, but the durable root name is
    # itself blank/missing -- fall back to the existing artifact-derived
    # logic rather than collapsing to "(unnamed)" when real filenames exist.
    filenames = [f"Example.Release.part{index:02d}.rar" for index in range(1, 25)]
    result = normalized_transfer_display_name(
        filenames, root_name="   ", root_is_canonical_identity=True,
    )
    assert result == "Example.Release + 24 files"


def test_direct_link_batch_keeps_artifact_normalization_when_not_canonical_root():
    # root_is_canonical_identity defaults to False for generic/direct-link
    # sources -- useful artifact normalization must be preserved unchanged.
    filenames = [f"Example.Release.part{index:02d}.rar" for index in range(1, 25)]
    result = normalized_transfer_display_name(filenames, root_name="1fichier.com - abcdef123456")
    assert result == "Example.Release + 24 files"


def test_normalizer_never_needs_source_host_or_candidate_identity_arguments():
    import inspect

    signature = inspect.signature(normalized_transfer_display_name)
    for name in signature.parameters:
        assert "host" not in name
        assert "provider" not in name
        assert "candidate" not in name


# ── Pinned count-wording distinction (collection-identity vs representative-
#    filename form) -- both forms exercised with a 24-file set so the ONLY
#    variable is whether the shown name is a derived base (never itself one
#    of the N files -> count = total) or an actual filename (already visibly
#    one of the N files -> count = total - 1).


def test_pinned_collection_identity_form_uses_total_count():
    filenames = [f"Example.Release.part{index:02d}.rar" for index in range(1, 25)]
    assert (
        normalized_transfer_display_name(filenames)
        == "Example.Release + 24 files"
    )


def test_pinned_representative_filename_form_uses_total_minus_one_count():
    # Only the first file matches a recognized multipart pattern; the other
    # 23 do not share its base, so no safe common collection identity can be
    # derived and the normalizer conservatively falls back to showing that
    # first filename as-is plus how many OTHER files exist (23, not 24 --
    # the shown filename already accounts for one of the 24).
    filenames = ["Example.Release.part01.rar"] + [
        f"other-file-{index:02d}.dat" for index in range(2, 25)
    ]
    assert len(filenames) == 24
    assert (
        normalized_transfer_display_name(filenames)
        == "Example.Release.part01.rar + 23 files"
    )


# ── Read models: Details and the restart confirmation use the same title ──
#
# A Media Downloads URL ending in ``/watch`` keeps ``torrents.name`` "watch";
# its resolved artifact names the real title. The Details read model and the
# restart confirmation derive the same display name the Downloads list shows,
# from current canonical artifacts, never by rewriting the durable name.

import pytest

import api.operational_downloads as downloads
import db.database as database
from transfers.models import TransferRequest


async def _lab(tmp_path, monkeypatch, *, slots=1):
    from test_v113_downloads_queue_priority import queue_lab
    return await queue_lab(tmp_path, monkeypatch, slots=slots)


async def _watch(core, title=None, *, name="watch"):
    transfer = await core.engine.submit((TransferRequest("ledger", name, name=name),))
    await core.engine.tick()
    if title is not None:
        async with database.get_db() as db:
            await db.execute("UPDATE download_files SET filename=? WHERE torrent_id=?", (title, transfer.id))
            await db.commit()
    return transfer.id


async def _durable(transfer_id):
    async with database.get_db() as db:
        name = (await db.fetchone("SELECT name FROM torrents WHERE id=?", (transfer_id,)))["name"]
        requests = await db.fetchall("SELECT payload FROM transfer_requests WHERE transfer_id=? ORDER BY id",
                                     (transfer_id,))
    return name, [row["payload"] for row in requests]


async def _listed(core, transfer_id):
    page = await downloads.list_operational_torrents(status=None, search=None, limit=0, offset=0, order="queue",
                                                     application=core.application)
    return next(item["display_name"] for item in page["items"] if item["id"] == transfer_id)


async def _details(core, transfer_id):
    import api.routes as routes
    return await routes.get_torrent(transfer_id, application=core.application)


@pytest.mark.asyncio
async def test_details_names_a_watch_submission_by_its_resolved_title_without_rewriting_it(tmp_path, monkeypatch):
    core = await _lab(tmp_path, monkeypatch)
    pending = (await core.engine.submit((TransferRequest("ledger", "watch", name="watch"),))).id
    # Nothing resolved yet: the durable submitted name is the fallback.
    assert (await _details(core, pending)).get("display_name") == "watch"
    transfer = await _watch(core, "A Real Video Title [abc123].webm")
    before = await _durable(transfer)
    item = await _details(core, transfer)
    assert item.get("display_name") == "A Real Video Title [abc123].webm"
    assert item.get("display_name") == await _listed(core, transfer)
    assert item["name"] == "watch" and await _durable(transfer) == before


@pytest.mark.asyncio
async def test_details_keeps_a_torrent_root_title_and_ignores_non_current_artifacts(tmp_path, monkeypatch):
    core = await _lab(tmp_path, monkeypatch)
    # A torrent/magnet root title is the transfer's identity, whatever its members are called.
    torrent = await _watch(core, "member.file.mkv", name="Example Release")
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET payload=json_set(payload,'$.kind','magnet') "
                         "WHERE transfer_id=? AND parent_id IS NULL", (torrent,))
        await db.commit()
    assert (await _details(core, torrent)).get("display_name") == "Example Release" == await _listed(core, torrent)

    # A blocked or standby row is never a current member: it cannot name the transfer.
    media = await _watch(core, "The Real Title.webm")
    async with database.get_db() as db:
        for ordinal, (column, value) in enumerate((("blocked", 1), ("mirror_state", "standby")), 1):
            request = f"misleading-{column}"
            await db.execute(
                "INSERT INTO transfer_requests (id, transfer_id, parent_id, ordinal, payload) "
                "SELECT ?, transfer_id, id, ?, payload FROM transfer_requests "
                "WHERE transfer_id=? AND parent_id IS NULL", (request, ordinal, media))
            await db.execute(
                f"INSERT INTO download_files (torrent_id, request_id, filename, status, {column}) "
                "SELECT torrent_id, ?, 'Misleading.mkv', status, ? FROM download_files "
                "WHERE torrent_id=? ORDER BY id LIMIT 1", (request, value, media))
        await db.commit()
    assert (await _details(core, media)).get("display_name") == "The Real Title.webm" == await _listed(core, media)
