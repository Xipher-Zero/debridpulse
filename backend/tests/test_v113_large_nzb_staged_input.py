"""1.0.13 release blocker G/H/I/J/K: large submitted input.

Measured on this tree before the correction, with representative manifests
(40 segments per file, realistic subjects and message identifiers):

    manifest   files    segments    peak RSS (was)   candidate context (was)
     33 MiB    6,934     277,360        235.5 MiB          46,138,562 chars
    100 MiB   20,904     836,160        682.4 MiB         139,812,154 chars
    256 MiB   53,373   2,134,920       1721.4 MiB         357,917,250 chars

The same payload was base64-expanded and persisted TWICE -- once in the durable
request row, once in candidate context -- and a 16 MiB ceiling in the parser,
the route and the browser (plus a 20 MiB general body ceiling) rejected all
three before any of that could happen.

The correction is a neutral durable-input owner plus a bounded reader, which
these tests hold to: the payload never enters request or candidate JSON, the
reader's memory does not scale with the manifest, and the input is integrity
checked, restart-safe and reclaimed by one owner.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap

import pytest

from providers.usenet.nzb import InvalidNzb, read
from providers.usenet.provider import CONTEXT_STAGED_INPUT, UsenetProvider
from transfers import codec
from transfers.models import TransferRequest
from transfers.staged_input import (
    MAX_STAGED_INPUT_BYTES, StagedInputError, StagedInputStore, StagedPayload,
)

MIB = 1024 * 1024


def synthetic_nzb(target_bytes: int) -> bytes:
    """A well-formed NZB of approximately ``target_bytes``."""
    parts = ['<?xml version="1.0" encoding="utf-8"?>\n'
             '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n']
    written = len(parts[0])
    index = 0
    while written < target_bytes - 16:
        index += 1
        segments = "".join(
            f'<segment bytes="768000" number="{number}">'
            f'part{index}.{number}.{"a" * 40}@news.example.invalid</segment>\n'
            for number in range(1, 41))
        block = (f'<file poster="poster@example.invalid" date="1700000000" '
                 f'subject="[{index}/999] &quot;Collection.Member.{index:05d}.r00&quot; '
                 f'yEnc (1/40)">\n<groups><group>alt.binaries.test</group></groups>\n'
                 f'<segments>\n{segments}</segments>\n</file>\n')
        parts.append(block)
        written += len(block)
    parts.append("</nzb>\n")
    return "".join(parts).encode("utf-8")


@pytest.fixture
def store(tmp_path):
    return StagedInputStore(str(tmp_path / "staged"))


async def stage_bytes_async(store, payload: bytes) -> StagedPayload:
    async def chunks():
        for offset in range(0, len(payload), MIB):
            yield payload[offset:offset + MIB]
    return await store.stage(chunks())


# --- C1/C2/C3: the size contract -------------------------------------------

@pytest.mark.parametrize("megabytes", [33, 100])
def test_c1_c2_a_large_manifest_is_accepted_by_the_reader(megabytes):
    manifest = read(io.BytesIO(synthetic_nzb(megabytes * MIB)))
    assert manifest.file_count > 0 and manifest.segment_count > 0
    assert manifest.declared_bytes > 0


def test_c3_the_product_contract_admits_at_least_256_mib():
    assert MAX_STAGED_INPUT_BYTES >= 256 * MIB


def test_c3_the_streamed_upload_path_is_not_bound_by_the_general_body_ceiling():
    """A 33 MiB NZB used to be refused by the general 20 MiB body ceiling,
    before the route was ever reached."""
    import main
    assert main.STAGED_UPLOAD_PATH == "/api/usenet/add-file"
    source = open(main.__file__).read()
    assert "limit = MAX_STAGED_INPUT_BYTES" in source
    assert main._MAX_REQUEST_BODY_BYTES < MAX_STAGED_INPUT_BYTES, \
        "the staged upload must be governed by its own owner's ceiling"


def test_c3_no_16_mib_nzb_assumption_survives_anywhere():
    backend = _backend_dir()
    route = (backend / "api" / "routes.py").read_text()
    nzb_route = route.split("async def add_usenet_file")[1].split("@router.post")[0]
    assert "16 * 1024 * 1024" not in nzb_route
    parser = (backend / "providers" / "usenet" / "nzb.py").read_text()
    assert "MAX_NZB_BYTES" not in parser and "16 * 1024 * 1024" not in parser
    browser = (backend.parent / "frontend" / "static" / "app.js").read_text()
    upload = browser.split("async function uploadTransferFile")[1][:1500]
    assert "maxBytes" in upload, "the browser must judge each upload kind by its own ceiling"


def main_path():
    import main
    return main.__file__


# --- C4: no payload in candidate JSON --------------------------------------

@pytest.mark.asyncio
async def test_c4_candidate_context_carries_a_reference_not_the_payload(store):
    payload = synthetic_nzb(33 * MIB)
    reference = await stage_bytes_async(store, payload)
    result = await UsenetProvider(staged_input=store).resolve(
        TransferRequest("nzb", reference, name="big.nzb"))

    context = json.dumps(result.candidates[0].context)
    assert len(context) < 1024, f"candidate context grew with the payload: {len(context)}"
    assert "nzb_base64" not in context
    assert payload[:64].decode("utf-8", "ignore") not in context
    assert result.candidates[0].context[CONTEXT_STAGED_INPUT]["sha256"] == reference.sha256


@pytest.mark.asyncio
async def test_c4_the_durable_request_row_carries_a_reference_not_the_payload(store):
    reference = await stage_bytes_async(store, synthetic_nzb(33 * MIB))
    row = codec.dump(TransferRequest("nzb", reference, name="big.nzb"))
    assert len(row) < 1024, f"the request row grew with the payload: {len(row)}"
    assert "$bytes" not in row
    restored = codec.request(codec.load(row))
    assert restored.payload == reference


# --- C5: restart before native submission ----------------------------------

@pytest.mark.asyncio
async def test_c5_the_exact_input_survives_a_restart_before_submission(tmp_path):
    payload = synthetic_nzb(4 * MIB)
    original = StagedInputStore(str(tmp_path / "staged"))
    reference = await stage_bytes_async(original, payload)
    row = codec.dump(TransferRequest("nzb", reference, name="big.nzb"))

    # A new process: nothing in memory survives, only the row and the files.
    reborn = StagedInputStore(str(tmp_path / "staged"))
    restored = codec.request(codec.load(row))
    assert reborn.read(restored.payload) == payload


# --- C6: integrity ----------------------------------------------------------

@pytest.mark.asyncio
async def test_c6_a_tampered_staged_input_fails_closed(store, tmp_path):
    reference = await stage_bytes_async(store, b"<nzb><file/></nzb>")
    path = tmp_path / "staged" / f"{reference.id}.input"
    path.write_bytes(b"<nzb><file/></nzb>".replace(b"file", b"evil"))
    with pytest.raises(StagedInputError):
        store.read(reference)


@pytest.mark.asyncio
async def test_c6_a_truncated_staged_input_fails_closed(store, tmp_path):
    payload = synthetic_nzb(1 * MIB)
    reference = await stage_bytes_async(store, payload)
    (tmp_path / "staged" / f"{reference.id}.input").write_bytes(payload[:-64])
    with pytest.raises(StagedInputError):
        store.read(reference)


def test_c6_a_missing_staged_input_is_an_error_not_an_empty_read(store):
    absent = StagedPayload("0" * 32, "a" * 64, 10)
    with pytest.raises(StagedInputError):
        store.read(absent)


def test_c6_a_reference_can_never_express_a_path(store):
    for identity in ("../escape", "/etc/passwd", "a" * 31, "", "A" * 32, "x/y"):
        with pytest.raises(StagedInputError):
            StagedPayload(identity, "a" * 64, 1)


@pytest.mark.asyncio
async def test_c6_the_ceiling_is_enforced_while_writing(tmp_path):
    small = StagedInputStore(str(tmp_path / "staged"), max_bytes=4096)
    async def chunks():
        for _ in range(10):
            yield b"x" * 1024
    with pytest.raises(StagedInputError):
        await small.stage(chunks())
    assert not list((tmp_path / "staged").glob("*.input")), \
        "a refused upload must leave nothing behind"


# --- C7/C8: reclamation -----------------------------------------------------

@pytest.mark.asyncio
async def test_c7_a_referenced_input_is_never_reclaimed(store):
    reference = await stage_bytes_async(store, b"<nzb/>")
    assert store.sweep({reference.id}, grace_seconds=0) == 0
    assert store.read(reference) == b"<nzb/>"


@pytest.mark.asyncio
async def test_c7_an_unreferenced_input_is_reclaimed(store):
    reference = await stage_bytes_async(store, b"<nzb/>")
    assert store.sweep(set(), grace_seconds=0) == 1
    with pytest.raises(StagedInputError):
        store.read(reference)


@pytest.mark.asyncio
async def test_c7_the_grace_window_protects_an_input_not_yet_durable(store):
    """Staged, but the transfer that will reference it does not exist yet."""
    reference = await stage_bytes_async(store, b"<nzb/>")
    assert store.sweep(set()) == 0, "a fresh staged input must survive the sweep"
    assert store.read(reference) == b"<nzb/>"


@pytest.mark.asyncio
async def test_c8_a_crash_during_staging_leaks_nothing_forever(tmp_path):
    crashed = StagedInputStore(str(tmp_path / "staged"))
    async def chunks():
        yield b"x" * 32
        raise RuntimeError("the process died mid-upload")
    with pytest.raises(RuntimeError):
        await crashed.stage(chunks())
    (tmp_path / "staged").mkdir(exist_ok=True)
    leftover = list((tmp_path / "staged").iterdir())
    assert crashed.sweep(set(), grace_seconds=0) == len(leftover)
    assert not list((tmp_path / "staged").iterdir())


def test_c7_reclamation_has_exactly_one_owner():
    backend = _backend_dir()
    callers = []
    for path in backend.rglob("*.py"):
        if "tests" in path.parts:
            continue
        if ".sweep(" in path.read_text():
            callers.append(path.name)
    assert callers == ["service.py"], f"staged input must have one sweep caller: {callers}"


# --- C9: bounded parsing ----------------------------------------------------

@pytest.mark.parametrize("megabytes", [33, 100])
def test_c9_reader_memory_does_not_scale_with_the_manifest(megabytes, tmp_path):
    """A qualification probe in a fresh interpreter, with a documented bound.

    The document-tree reader cost 235.5 MiB for 33 MiB and 682.4 MiB for
    100 MiB -- roughly 6.8x the manifest. The bounded reader measured 15.6 and
    16.9 MiB. The bound below is generous enough not to be flaky while still
    failing decisively if whole-document or base64 behaviour ever returns.
    """
    path = tmp_path / "probe.nzb"
    path.write_bytes(synthetic_nzb(megabytes * MIB))
    programme = textwrap.dedent(f"""
        import resource, sys
        sys.path.insert(0, {str(_backend_dir())!r})
        from providers.usenet.nzb import read
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        with open({str(path)!r}, "rb") as handle:
            manifest = read(handle)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(after - before, manifest.file_count, manifest.segment_count)
    """)
    output = subprocess.run([sys.executable, "-c", programme], capture_output=True,
                            text=True, check=True).stdout.split()
    growth_bytes = int(output[0]) * 1024
    assert growth_bytes < 64 * MIB, (
        f"reading a {megabytes} MiB manifest grew resident memory by "
        f"{growth_bytes / MIB:.1f} MiB")


def _backend_dir():
    import pathlib
    return pathlib.Path(main_path()).parent


def test_c9_no_whole_payload_representation_survives_in_the_maintained_path():
    backend = _backend_dir()
    parser = (backend / "providers" / "usenet" / "nzb.py").read_text()
    assert "iterparse" in parser, "the reader must stream"
    assert "fromstring" not in parser, "no whole-document parse may remain"
    assert "element.clear()" in parser, "elements must be released as they are consumed"
    # Prose may still explain what was removed; no CODE may re-create it.
    provider = (backend / "providers" / "usenet" / "provider.py").read_text()
    assert "import base64" not in provider
    assert "base64." not in provider
    assert "nzb_base64" not in provider.replace("base64 in a context field", "")

    # The executor decodes base64 in exactly ONE place: the one-way first-use
    # convergence of work resolved before this architecture existed
    # (DP 1.0.13 Item 9). That path runs at most once per transfer, is bounded
    # by the request-body ceiling that applied when the work was submitted, and
    # ends by writing a staged reference -- after which the ordinary streamed
    # path is the only one that runs. What must never come back is a whole
    # payload in the MAINTAINED path, so that is what is asserted.
    executor = (backend / "executors" / "sabnzbd" / "executor.py").read_text()
    assert executor.count("b64decode(") == 1, "more than one whole-payload decode"
    assert "b64encode" not in executor, "the executor must never re-create the inline form"
    decode = executor.index("b64decode(")
    converge = executor.index("async def _converge_obsolete_input(")
    assert decode > converge, "the decode escaped the one-way convergence"
    following = executor[converge:decode + 2000]
    assert "addfile" not in following, "decoded bytes reach the native submission"
    # And the canonical resolution of an input never decodes anything: it
    # returns a reference, or hands the obsolete case to the migration.
    staged = executor[executor.index("    async def _staged("):converge]
    assert "b64decode" not in staged
    assert "StagedPayload.from_context(raw)" in staged


def test_c9_defensive_validation_was_not_weakened_to_gain_streaming():
    rejected = [
        b"", b"<x/>", b"<nzb></nzb>",
        b'<nzb><file subject="a"><segments></segments></file></nzb>',
        b'<nzb><file subject="&quot;x.bin&quot;"><segments>'
        b'<segment bytes="z">m@x</segment></segments></file></nzb>',
        b'<nzb><file subject="&quot;x.bin&quot;"><segments>'
        b'<segment bytes="-1">m@x</segment></segments></file></nzb>',
        b'<nzb><file subject="&quot;x.bin&quot;"><segments>'
        b'<segment bytes="1"></segment></segments></file></nzb>',
        b'<nzb><file subject="&quot;x.bin&quot;"><segments>'
        b'<segment bytes="1">m@x</segment></segments></file>',
        b'<nzb><file subject="no quoted name"><segments>'
        b'<segment bytes="1">m@x</segment></segments></file></nzb>',
    ]
    for payload in rejected:
        with pytest.raises(InvalidNzb):
            read(io.BytesIO(payload))


def test_c9_an_external_entity_is_never_resolved():
    hostile = (b'<?xml version="1.0"?><!DOCTYPE nzb [<!ENTITY xxe SYSTEM '
               b'"file:///etc/passwd">]><nzb><file subject="&quot;&xxe;&quot;">'
               b'<segments><segment bytes="1">m@x</segment></segments></file></nzb>')
    with pytest.raises(InvalidNzb):
        read(io.BytesIO(hostile))
