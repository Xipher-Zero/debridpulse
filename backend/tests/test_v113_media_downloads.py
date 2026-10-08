"""Media Downloads (yt-dlp): the contract layer -- applicability, routing,
policy, ownership and presentation, with no network and no native tool.

Every proof here uses the installed yt-dlp registry and selector themselves
(pure, offline) or a fake extraction; the sandboxed worker, the egress guard,
the executor's process lifecycle and the real finalizers are proven in
``test_v113_media_downloads_runtime``.
"""
from __future__ import annotations

import asyncio
import json
import socket

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor import gen_extractor_classes, list_extractor_classes

from core.config import AppSettings
from executors.media import worker
from executors.media.executor import MediaExecutor
from integrations.catalog import definitions as catalog
from integrations.configuration import effective_integration_settings, normalize_settings, public_integrations
from integrations.definition import IntegrationEnvironment, IntegrationGroupSettings, IntegrationSettings
from integrations.media.definition import definition as media_definition
from integrations.media.outcomes import OUTCOMES, MediaFailure, outcome_error
from providers.general_http.definition import definition as http_definition
from providers.media import plan as planning
from providers.media.provider import MEMBER_KIND, PLAN_KEY, MediaProvider
from test_v113_collection_route_generic_closure import Route, by_payload, drive, lab, routes, submit
from transfers import codec
from transfers.applicability import ApplicabilityClass
from transfers.errors import Category, Stage, TransferError
from transfers.models import (
    ContinuationCapability, Endpoint, ExecutionSubject, ExecutionWork, MaterializationKind, MaterializationPlan,
    ExecutionRequest, TransferCandidate, TransferRequest,
)
from transfers.policy import provider_attributable
from transfers.registry import IntegrationRegistry

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SHORT = "https://youtu.be/dQw4w9WgXcQ"
PLAYLIST = "https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Ar_3GPy3workQZiTQKzxs"
PLAIN = ("https://files.example.org/archive.zip", "https://cdn.example.org/clip.mp4", "https://example.com/")


def media_facts(**overrides):
    facts = {"kind": "media", "extractor": "Youtube", "id": "dQw4w9WgXcQ", "title": "Clip", "live_status": "",
             "formats": [{"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none",
                          "protocol": "https", "height": 1080, "width": 1920},
                         {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
                          "protocol": "https", "height": None, "width": None}],
             "subtitles": {}, "automatic_captions": {}}
    facts.update(overrides)
    return facts


class Extraction:
    """A fake of the worker's read-only extraction (``MediaSandbox.extract``)."""

    def __init__(self, answer=None):
        self.answer = answer if answer is not None else media_facts()
        self.calls = []

    async def __call__(self, url, *, selection, collection_bound):
        self.calls.append((url, selection, collection_bound))
        if isinstance(self.answer, MediaFailure):
            raise self.answer
        return self.answer(url) if callable(self.answer) else self.answer


# -- applicability ------------------------------------------------------------------------

def test_only_an_explicit_installed_extractor_claims_in_native_dispatch_order(monkeypatch):
    native = tuple(gen_extractor_classes())
    assert planning._extractor_classes() == native
    assert native[-1].ie_key() == "Generic"
    # The display list is sorted; the dispatch order is not that list.
    assert [item.ie_key() for item in list_extractor_classes()] != [item.ie_key() for item in native]
    for url in (VIDEO, SHORT, PLAYLIST, "https://vimeo.com/76979871"):
        expected = next(item.ie_key() for item in native if item.suitable(url))
        assert planning.explicit_extractor(url) == expected != "Generic"
    for url in PLAIN:
        # GenericIE would take these; it is never a claimant.
        assert next(item.ie_key() for item in native if item.suitable(url)) == "Generic"
        assert planning.explicit_extractor(url) is None

    def no_network(*_args, **_kwargs):
        raise AssertionError("applicability performed network I/O")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    provider = MediaProvider(Extraction())
    claim = provider.applicability_for(TransferRequest("https", "https://m.youtube.com/watch?v=xyzxyzxyz12"))
    assert claim.is_specialized and claim.collection_authority is False
    assert [item.host for item in claim.specialized_hosts] == ["m.youtube.com"]
    for url in PLAIN:
        assert not provider.applicability_for(TransferRequest("https", url)).is_specialized
    member = provider.applicability_for(TransferRequest(MEMBER_KIND, VIDEO))
    assert member.collection_authority is False and not member.is_specialized


def _registry(*items):
    registry = IntegrationRegistry()
    for item in items:
        registry.register_provider(item)
    return registry


def test_generic_http_keeps_every_address_media_downloads_does_not_claim():
    registry = _registry(MediaProvider(Extraction()), http_definition.build(IntegrationSettings(), None))
    for url in PLAIN:
        assert [item.descriptor.id for item in registry.eligible_providers(TransferRequest("https", url))] == [
            "general_http"]
    assert [item.descriptor.id for item in registry.eligible_providers(TransferRequest("https", VIDEO))] == ["media"]
    # A media claim speaks only for its own root: it never closes generic
    # competition for the independent roots pasted beside it.
    assert registry.collection_route_authority(
        (TransferRequest("https", VIDEO), *(TransferRequest("https", url) for url in PLAIN))) is False


@pytest.mark.asyncio
async def test_a_mixed_paste_routes_each_root_through_its_own_claimant(tmp_path, monkeypatch):
    media = MediaProvider(Extraction(lambda url: media_facts()))
    generic = Route("generic-route", generic=True)
    authority = Route("special-x", hosts=("special.test",))
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, media, generic, authority)
    files = ["https://files.example.org/a.bin", "https://files.example.org/b.bin"]
    transfer = await submit(engine, VIDEO, *files)

    await drive(engine)

    assert await repository.collection_route_authority(transfer.id) is False
    assert sorted(generic.resolved) == sorted(files)
    history = await routes(repository, transfer.id)
    assert sorted(item["provider_id"] for item in history) == ["generic-route", "generic-route", "media"]
    roots = await by_payload(repository, transfer.id)
    assert roots[VIDEO].state in {"resolved", "materializing"}

    # Unchanged for an authority-bearing specialized claimant: its paste still
    # closes generic competition for every root.
    second = await submit(engine, "https://special.test/c.bin", VIDEO, "https://files.example.org/d.bin")
    await drive(engine)
    assert await repository.collection_route_authority(second.id) is True
    assert "https://files.example.org/d.bin" not in generic.resolved
    assert (await by_payload(repository, second.id))[VIDEO].state in {"resolved", "materializing"}


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["unavailable", "extractor_failed", "network", "auth_required",
                                  "runtime_unavailable", "egress_refused"])
async def test_a_claimed_medium_never_falls_through_to_generic_http(tmp_path, monkeypatch, code):
    media = MediaProvider(Extraction(MediaFailure(code)))
    generic = Route("generic-route", generic=True)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, media, generic)
    transfer = await submit(engine, VIDEO)

    await drive(engine)

    assert generic.resolved == []
    root = (await by_payload(repository, transfer.id))[VIDEO]
    assert await repository.exhausted_route_providers(root.id) == frozenset()
    assert {item["provider_id"] for item in await routes(repository, transfer.id)} == {"media"}


def test_no_outcome_is_ever_attributed_to_a_replaceable_provider():
    for code in OUTCOMES:
        for stage in (Stage.RESOLUTION, Stage.EXECUTION):
            assert not provider_attributable(outcome_error(code, stage)), code
    assert not provider_attributable(outcome_error("never-heard-of", Stage.RESOLUTION))


# -- integration, enable and status -------------------------------------------------------

class _Repository:
    async def authorize_execution(self, *_args):
        return True


def test_one_paired_integration_one_enable_state_and_network_sources_membership():
    environment = IntegrationEnvironment(_Repository(), "/tmp/dp-media-downloads")
    assert media_definition.kind == "provider_executor" and media_definition.secret_fields == frozenset()
    assert media_definition.owned_identities == frozenset({"media", "yt_dlp"})
    provider, executor = media_definition.build(IntegrationSettings(), environment)
    assert provider.descriptor.enabled and executor.descriptor.enabled

    saved = AppSettings(integrations={"media": IntegrationSettings(enabled=False,
                                                                   options={"target_resolution": "720"})})
    settings = normalize_settings(saved, catalog)
    provider, executor = media_definition.build(effective_integration_settings(settings, media_definition),
                                                environment)
    assert not provider.descriptor.enabled and not executor.descriptor.enabled
    assert settings.integrations["media"].options == {"target_resolution": "720"}
    registry = _registry(provider, http_definition.build(IntegrationSettings(), None))
    assert [item.descriptor.id for item in registry.eligible_providers(TransferRequest("https", VIDEO))] == [
        "general_http"]

    # The Network Sources gate governs it exactly like every other member,
    # without rewriting its own namespace.
    gated = normalize_settings(AppSettings(integration_groups={"direct_sources": IntegrationGroupSettings(
        enabled=False)}), catalog)
    assert effective_integration_settings(gated, media_definition).enabled is False
    assert gated.integrations["media"].enabled is True

    presentation = public_integrations(normalize_settings(AppSettings(), catalog), catalog)["media"]["presentation"]
    members = sorted((item["presentation"]["display_order"], name) for name, item in public_integrations(
        normalize_settings(AppSettings(), catalog), catalog).items()
        if item["presentation"]["status_group"] == "direct_sources")
    assert members[-1] == (916, "media")
    assert presentation["status_name"] == "Media Downloads"
    assert presentation["status_group_label"] == "Network Sources"
    assert presentation["status_endpoint"] == "/integration-status/media"
    assert (presentation["transfer_label"], presentation["transfer_theme"]) == ("Media Download", "hot-rose")


@pytest.mark.asyncio
async def test_status_reports_runtime_readiness_truthfully():
    from types import SimpleNamespace

    from api import settings_validation_routes as status

    class Executor:
        def __init__(self, ready):
            self.ready = ready

        async def health(self):
            return SimpleNamespace(ready=self.ready)

    def application(ready):
        return SimpleNamespace(engine=SimpleNamespace(registry=SimpleNamespace(executors={"yt_dlp": Executor(ready)})))

    # Readiness only: participation is the settings document's fact, which
    # the status surface projects without asking a disabled member's endpoint.
    assert await status.get_media_runtime_status(application(True)) == {"state": "healthy"}
    assert await status.get_media_runtime_status(application(False)) == {"state": "unavailable"}


# -- acquisition policy -------------------------------------------------------------------

def _selected(heights, target, *extra):
    def video(height, protocol="https"):
        return {"format_id": f"{protocol}-{height}", "url": f"https://cdn.test/{height}", "height": height,
                "width": height * 16 // 9, "vcodec": "avc1", "acodec": "none", "ext": "mp4", "protocol": protocol}
    audio = {"format_id": "audio", "url": "https://cdn.test/a", "vcodec": "none", "acodec": "mp4a.40.2",
             "ext": "m4a", "protocol": "https"}
    formats = [video(height) for height in heights] + [video(*item) for item in extra] + [audio]
    chosen = planning.selection(target)
    with YoutubeDL({"quiet": True, "format": chosen["format"], "format_sort": chosen["format_sort"]}) as ydl:
        result = ydl.process_ie_result({"id": "x", "title": "x", "extractor_key": "Test",
                                        "webpage_url": "https://t/x", "formats": formats}, download=False)
    return [item["format_id"] for item in result.get("requested_formats") or [result]]


def test_target_resolution_is_exact_then_largest_below_then_smallest_above():
    assert _selected([2160, 1440, 1080, 720], "1080") == ["https-1080", "audio"]
    assert _selected([2160, 1440, 720], "1080") == ["https-720", "audio"]
    assert _selected([2160, 1440], "1080") == ["https-1440", "audio"]
    # Not numeric-nearest: 1440 is nearer to 1080 than 480 is, and still loses.
    assert _selected([1440, 480], "1080") == ["https-480", "audio"]
    # Best Available has no ceiling.
    assert _selected([4320, 2160, 1080], "best") == ["https-4320", "audio"]
    # Only transports carried over the guarded HTTP(S) route are ever selected.
    assert _selected([1080], "best", (4320, "rtmp"), (2160, "rtsp"), (1440, "m3u8")) == ["https-1080", "audio"]
    assert _selected([1080, 480], "720", (720, "m3u8_native")) == ["m3u8_native-720", "audio"]
    assert media_definition.options_model().target_resolution == "best"


def test_audio_only_media_is_one_native_file():
    facts = media_facts(formats=[{"format_id": "251", "ext": "opus", "vcodec": "none", "acodec": "opus",
                                  "protocol": "https", "height": None, "width": None}])
    plan = planning.plan(facts, url=VIDEO, target="1080", subtitle_language="en")
    assert (plan["container"], plan["formats"], plan["subtitle"]) == ("opus", ["251"], None)


@pytest.mark.asyncio
async def test_live_and_authenticated_media_are_refused_without_any_credential_flow():
    live = MediaProvider(Extraction(media_facts(live_status="is_live")))
    with pytest.raises(TransferError) as raised:
        await live.resolve(TransferRequest("https", VIDEO))
    assert (raised.value.error.category, raised.value.error.native_code) == (Category.UNSUPPORTED_REQUEST,
                                                                             "live_unsupported")
    private = MediaProvider(Extraction(MediaFailure("auth_required")))
    with pytest.raises(TransferError) as raised:
        await private.resolve(TransferRequest("https", VIDEO))
    assert raised.value.error.category == Category.SOURCE_UNAVAILABLE
    # Nothing can ask for, carry or store a cookie or login.
    assert not hasattr(private, "resolve_with_input")
    assert set(media_definition.options_model.model_fields) == {"target_resolution"}
    params = worker._params({"proxy": "http://p"}, worker._Log())
    assert (params["cookiefile"], params["usenetrc"], params["remote_components"]) == (None, False, [])


def test_container_is_native_when_it_carries_everything_and_mkv_only_when_it_cannot():
    mp4, m4a = media_facts()["formats"]
    vp9 = {"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "protocol": "https", "height": 1080}
    opus = {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "protocol": "https"}
    english_vtt = {"language": "en", "kind": "authored", "exts": ("json3", "vtt")}
    assert planning.container_plan([mp4, m4a], None) == ("mp4", None)
    assert planning.container_plan([vp9, opus], None) == ("webm", None)
    assert planning.container_plan([vp9, opus], english_vtt) == (
        "webm", {"language": "en", "kind": "authored", "ext": "vtt"})
    # MP4 carries no source subtitle without conversion: MKV, never mov_text.
    assert planning.container_plan([mp4, m4a], english_vtt) == (
        "mkv", {"language": "en", "kind": "authored", "ext": "vtt"})
    assert planning.container_plan([mp4, m4a], {**english_vtt, "exts": ("srt", "vtt")})[1]["ext"] == "srt"
    # No preferred-language track at all: nothing to embed, native kept.
    assert planning.container_plan([mp4, m4a], None) == ("mp4", None)
    # A track that exists but no container carries unchanged is never dropped
    # and never converted: the plan fails, truthfully.
    with pytest.raises(ValueError, match="subtitle_unembeddable"):
        planning.container_plan([mp4, m4a], {**english_vtt, "exts": ("json3", "ttml")})
    # A native container this integration cannot rewrite still embeds the
    # track unchanged -- in MKV.
    flv = {"format_id": "f", "ext": "flv", "vcodec": "h264", "acodec": "aac", "protocol": "https"}
    assert planning.container_plan([flv], {**english_vtt, "exts": ("srt",)}) == (
        "mkv", {"language": "en", "kind": "authored", "ext": "srt"})
    # Mismatched streams: yt-dlp's own compatibility answer is MKV.
    assert planning.container_plan([mp4, opus], None) == ("mkv", None)
    plan = planning.plan(media_facts(subtitles={"en": ["vtt"]}), url=VIDEO, target="best", subtitle_language="en")
    assert plan["container"] == "mkv"
    assert planning.file_name("Clip", "dQw4w9WgXcQ", plan["container"]).endswith(" [dQw4w9WgXcQ].mkv")


def test_subtitle_is_authored_then_generated_in_the_preferred_language_only():
    both = media_facts(subtitles={"en": ["vtt"], "fr": ["vtt"]}, automatic_captions={"en": ["vtt"], "de": ["vtt"]})
    assert planning.choose_subtitle(both, "en")["kind"] == "authored"
    generated = media_facts(subtitles={"fr": ["vtt"]}, automatic_captions={"en": ["vtt"], "es": ["vtt"]})
    assert planning.choose_subtitle(generated, "en") == {"language": "en", "kind": "generated", "exts": ("vtt",)}
    assert planning.choose_subtitle(media_facts(subtitles={"fr": ["vtt"]}, automatic_captions={"de": ["vtt"]}),
                                    "en") is None
    assert planning.choose_subtitle(media_facts(subtitles={"en-GB": ["srt"]}), "en")["language"] == "en-GB"
    assert planning.choose_subtitle(media_facts(subtitles={"pt": ["srt"]}), "pt-br")["language"] == "pt"
    # One track, never every language.
    plan = planning.plan(both, url=VIDEO, target="best", subtitle_language="en")
    assert plan["subtitle"] == {"language": "en", "kind": "authored", "ext": "vtt"}

    # A GLOBAL Downloads preference, English by default, consumed -- never
    # copied into the integration's own options.
    assert AppSettings().preferred_subtitle_language == "en"
    assert IntegrationEnvironment(None, "/tmp").preferred_subtitle_language == "en"
    settings = normalize_settings(AppSettings(preferred_subtitle_language="DE"), catalog)
    assert settings.preferred_subtitle_language == "de"
    assert "subtitle" not in json.dumps(settings.integrations["media"].model_dump())
    provider, _executor = media_definition.build(IntegrationSettings(), IntegrationEnvironment(
        _Repository(), "/tmp/dp-media-downloads", preferred_subtitle_language="de"))
    assert provider.subtitle_language == "de"
    with pytest.raises(ValueError):
        AppSettings(preferred_subtitle_language="english please")


@pytest.mark.asyncio
async def test_an_unembeddable_preferred_subtitle_fails_the_medium_instead_of_vanishing():
    authored_only_ttml = media_facts(subtitles={"en": ["ttml", "json3"]}, automatic_captions={"en": ["vtt"]})
    with pytest.raises(TransferError) as raised:
        await MediaProvider(Extraction(authored_only_ttml)).resolve(TransferRequest("https", VIDEO))
    error = raised.value.error
    assert (error.native_code, error.category) == ("subtitle_unembeddable", Category.NO_TRANSFER_CANDIDATE)
    assert not provider_attributable(error)
    # Authored still wins over generated: the generated vtt is not a quiet substitute.
    assert planning.choose_subtitle(authored_only_ttml, "en")["kind"] == "authored"


@pytest.mark.asyncio
async def test_one_planned_file_candidate_carries_no_volatile_material():
    signed = "https://rr1---sn-abc.googlevideo.com/videoplayback?expire=1&sig=SECRET"
    facts = media_facts(subtitles={"en": ["vtt"]})
    facts["formats"][0]["url"] = signed  # whatever the worker reported, only facts are kept
    provider = MediaProvider(Extraction(facts), target_resolution="1080", subtitle_language="en")
    result = await provider.resolve(TransferRequest("https", VIDEO))
    (candidate,) = result.candidates
    plan = candidate.context[PLAN_KEY]
    assert candidate.endpoints == () and candidate.materialization == MaterializationKind.FILE
    assert candidate.name == "Clip [dQw4w9WgXcQ].mkv" and plan["container"] == "mkv"
    assert (candidate.source_identity.scope, candidate.source_identity.key) == ("media", "Youtube:dQw4w9WgXcQ")
    assert plan["formats"] == ["137", "140"] and plan["url"] == VIDEO
    assert (plan["target_resolution"], plan["selected_height"], plan["subtitle_language"]) == ("1080", 1080, "en")
    persisted = codec.dump(candidate)
    assert "googlevideo" not in persisted and "SECRET" not in persisted and "sig=" not in persisted


@pytest.mark.asyncio
async def test_a_playlist_is_one_complete_manifest_whose_member_names_are_final():
    members = [{**media_facts(id="a1", title="First"), "url": "https://www.youtube.com/watch?v=aaaaaaaaaa1"},
               {**media_facts(id="b2", title="Second", subtitles={"en": ["vtt"]}),
                "url": "https://www.youtube.com/watch?v=bbbbbbbbbb2"},
               {"url": "https://www.youtube.com/watch?v=cccccccccc3", "id": "c3", "title": "Gone",
                "outcome": "unavailable"},
               {**media_facts(id="a1", title="First"), "url": "https://www.youtube.com/watch?v=aaaaaaaaaa1"}]
    extraction = Extraction({"kind": "collection", "extractor": "YoutubeTab", "id": "PL1", "title": "Mix",
                             "members": members})
    provider = MediaProvider(extraction)
    result = await provider.resolve(TransferRequest("https", PLAYLIST))
    assert extraction.calls[0][2] == planning.COLLECTION_BOUND
    assert result.candidates == () and result.observation.name == "Mix"
    names = [entry.relative_path for entry in result.observation.file_manifest.entries]
    assert names == ["First [a1].mp4", "Second [b2].mkv", "Gone [c3]"]
    assert all(entry.expected_bytes == 0 for entry in result.observation.file_manifest.entries)
    entries = await provider.manifest(result.observation.resource)
    assert [entry.request.kind for entry in entries] == [MEMBER_KIND] * 3
    assert [entry.relative_path for entry in entries] == names

    # A member resolves into exactly the file its manifest named ...
    member = MediaProvider(Extraction(media_facts(id="a1", title="First")))
    (candidate,) = (await member.resolve(entries[0].request)).candidates
    assert candidate.name == "First [a1].mp4"
    # ... and never into another container under that name.
    changed = MediaProvider(Extraction(media_facts(id="a1", title="First", subtitles={"en": ["vtt"]})))
    with pytest.raises(TransferError) as raised:
        await changed.resolve(entries[0].request)
    assert raised.value.error.native_code == "plan_changed"
    with pytest.raises(TransferError) as raised:
        await member.resolve(entries[2].request)
    assert raised.value.error.native_code == "unavailable"

    # The bound fails closed rather than truncating.
    with pytest.raises(worker.Failure) as refused:
        worker.bounded_entries({"entries": [{}] * 4}, 3)
    assert refused.value.code == "collection_too_large"
    assert len(worker.bounded_entries({"entries": [{}] * 3}, 3)) == 3
    assert outcome_error("collection_too_large", Stage.RESOLUTION).category == Category.UNSUPPORTED_REQUEST


# -- executor contract --------------------------------------------------------------------

def _candidate(**overrides):
    plan = planning.plan(media_facts(), url=VIDEO, target="best", subtitle_language="en")
    values = dict(name="Clip [dQw4w9WgXcQ].mp4", endpoints=(), provider_id="media", context={PLAN_KEY: plan},
                  request_kind="https")
    values.update(overrides)
    return TransferCandidate(**values)


def test_the_executor_claims_only_media_plans_and_promises_only_a_restart(tmp_path, monkeypatch):
    executor = MediaExecutor(str(tmp_path), str(tmp_path / "runtime"), _Repository().authorize_execution)
    # The worker inherits no ambient proxy, credential, home or configuration,
    # and its argv carries no instruction at all (that arrives on stdin).
    for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY", "XDG_CONFIG_HOME", "YTDLP_PLUGINS"):
        monkeypatch.setenv(name, "ambient")
    environment = executor.sandbox.environment(tmp_path / "workspace")
    assert set(environment) == {"PATH", "LC_ALL", "LANG", "HOME", "YTDLP_NO_PLUGINS", "NO_COLOR", "TMPDIR",
                                "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "DENO_DIR", "DENO_NO_UPDATE_CHECK"}
    assert "ambient" not in environment.values() and environment["HOME"].startswith(str(tmp_path / "runtime"))
    assert executor.sandbox.argv()[1:3] == ["-I", "-B"] and len(executor.sandbox.argv()) == 4
    assert executor.capabilities.continuation == frozenset({ContinuationCapability.FULL_RESTART})
    assert executor.capabilities.materialization_kinds == frozenset({MaterializationKind.FILE})
    assert not (executor.capabilities.per_execution_pause or executor.capabilities.native_assisted_retry
                or executor.capabilities.candidate_sampling or executor.capabilities.transient_input)
    assert executor.claim(ExecutionSubject.of(_candidate())).supported
    assert executor.claim(ExecutionSubject.of(_candidate(request_kind=MEMBER_KIND))).supported
    assert not executor.claim(ExecutionSubject.of(_candidate(context={}))).supported
    assert not executor.claim(ExecutionSubject.of(_candidate(endpoints=(Endpoint("https", VIDEO),)))).supported

    from executors.aria2.executor import Aria2Executor
    # The ordinary HTTP executor has nothing to claim in a media plan.
    assert Aria2Executor._endpoint(_candidate()) is None

    target = tmp_path / "Clip [dQw4w9WgXcQ].mp4"
    work = ExecutionWork(ExecutionSubject.of(_candidate()), MaterializationPlan(MaterializationKind.FILE,
                                                                                str(tmp_path), str(target)), "a1")
    assert executor.footprint(work).transient_trees == (str(tmp_path / ".Clip [dQw4w9WgXcQ].mp4.dp-media"),)
    assert executor.prepare(ExecutionRequest(work, "a1")).correlation["target"] == str(target.resolve())
    renamed = ExecutionWork(work.subject, MaterializationPlan(MaterializationKind.FILE, str(tmp_path),
                                                              str(tmp_path / "Clip.mkv")), "a1")
    with pytest.raises(TransferError) as raised:
        executor.prepare(ExecutionRequest(renamed, "a1"))
    assert raised.value.error.category == Category.PATH_POLICY_VIOLATION


def test_finalization_only_ever_copies_streams():
    tools = {"ffmpeg": "/usr/bin/ffmpeg"}
    video = ("/w/component-0.mp4", {"vcodec": "avc1", "acodec": "none"})
    audio = ("/w/component-1.m4a", {"vcodec": "none", "acodec": "mp4a.40.2"})
    native = worker.finalization_argv(tools, "mp4", [video, audio], None, {"title": "Clip", "description": "D"},
                                      "/w/output.mp4")
    webm = worker.finalization_argv(tools, "webm", [video], ("/w/subtitle.vtt", "en"), {}, "/w/output.webm")
    merged = worker.finalization_argv(tools, "mkv", [video, audio], ("/w/subtitle.srt", "en"),
                                      {"title": "Clip", "description": "D & <E>"}, "/w/output.mkv")
    # One finalizer for every container, Matroska included.
    for argv in (native, webm, merged):
        assert argv[0] == tools["ffmpeg"] and argv[argv.index("-c") + 1] == "copy"
        for forbidden in ("-c:v", "-c:a", "-c:s", "-vcodec", "-acodec", "-vf", "-af", "-filter_complex", "-s",
                          "-b:v", "-b:a", "-r", "-crf", "mov_text"):
            assert forbidden not in argv
    assert native.count("-protocol_whitelist") == 2 and native.count("-i") == 2
    assert native[native.index("-bsf:a") + 1] == "aac_adtstoasc"
    assert "description=D" in native and native[-2:] == ["mp4", "file:/w/output.mp4"]
    assert "-bsf:a" not in webm and "language=en" in webm
    assert merged[-2:] == ["matroska", "file:/w/output.mkv"] and "description=D & <E>" in merged
    assert "file:/w/subtitle.srt" in merged and "language=en" in merged
    assert merged[merged.index("-disposition:s:0") + 1] == "default"


def _http_error(status: int, headers: dict | None = None):
    import io

    from yt_dlp.networking import Response
    from yt_dlp.networking.exceptions import HTTPError
    return HTTPError(Response(io.BytesIO(b""), "https://media.test/videoplayback", headers or {}, status=status))


def test_a_refusal_is_the_cause_only_of_the_failure_it_raised():
    box = worker.Sandbox(("127.0.0.1", 9), {"ffmpeg": "/opt/dp/tools/ffmpeg"}, ("/w",), (), None)
    # yt-dlp's HLS downloader probes the PATH's ffmpeg (not the confined one);
    # it is refused, yt-dlp handles that and downloads natively.
    with pytest.raises(PermissionError) as probe:
        box("subprocess.Popen", (None, ["/usr/bin/ffmpeg", "-bsfs"], None, None))
    assert isinstance(probe.value, worker.SandboxRefusal) and probe.value.event == "subprocess"
    # A later, unrelated failure is classified from its own chain only.
    assert worker.classify(_http_error(403), acquire=True, phase="component").code == "source_refused"
    assert worker.classify(_http_error(503), acquire=True, phase="component").code == "network"
    # A refusal that IS the failure's cause, however deeply wrapped, still is
    # the policy refusal it was.
    for event, code in (("socket.connect", "egress_refused"), ("subprocess", "transport_unsupported"),
                        ("open", "path_refused")):
        try:
            try:
                box._refuse(event)
            except PermissionError as refused:
                try:
                    raise OSError("wrapped once") from refused
                except OSError as wrapped:
                    raise RuntimeError("wrapped twice") from wrapped
        except RuntimeError as failure:
            assert worker.classify(failure, acquire=True, phase="component").code == code, event


def test_a_remote_403_is_the_origins_refusal_only_while_fetching_planned_media():
    from yt_dlp.networking.exceptions import ProxyError

    from transfers.errors import Origin, Retryability
    from transfers.policy import (
        Recovery,
        RecoveryAction,
        RecoveryContext,
        TransferPolicy,
        recovery_action,
    )
    for phase in ("component", "subtitle"):
        assert worker.classify(_http_error(403), acquire=True, phase=phase).code == "source_refused"
    error = outcome_error("source_refused", Stage.EXECUTION, detail="HTTP Error 403: Forbidden")
    assert (error.origin, error.retryability, error.category) == (
        Origin.REMOTE_SOURCE, Retryability.BACKOFF, Category.CANDIDATE_REJECTED)
    # Core's existing bounded recovery: a timed retry (a fresh extraction and
    # acquisition), never a terminal verdict on the first refusal.
    assert recovery_action(error) == Recovery.BACKOFF
    decision = TransferPolicy().recover(error, RecoveryContext(execution_attempts=1), 1000.0)
    assert decision.action == RecoveryAction.BACKOFF and decision.retry_at > 1000.0
    # The guard's own refusals keep their meaning: a plain-HTTP answer says
    # so (Proxy-Status), an HTTPS refusal is the CONNECT tunnel's.
    guarded = _http_error(403, {"Proxy-Status": "debridpulse; error=destination_ip_prohibited"})
    assert worker.classify(guarded, acquire=True, phase="component").code == "egress_refused"
    tunnel = ProxyError("Tunnel connection failed: 403 Forbidden")
    assert worker.classify(tunnel, acquire=True, phase="subtitle").code == "egress_refused"
    # Only status 403 is the origin's refusal: every other status keeps the
    # mapping it has outside the media-fetch phases.
    for status in (401, 404, 407, 410, 429, 500, 503):
        for phase in ("component", "subtitle"):
            during = worker.classify(_http_error(status), acquire=True, phase=phase).code
            assert during == worker.classify(_http_error(status), acquire=True, phase="extract").code, status
            assert during != "source_refused", status
    assert worker.classify(_http_error(404), acquire=True, phase="component").code == "unavailable"
    assert worker.classify(_http_error(429), acquire=True, phase="component").code == "rate_limited"
    assert worker.classify(_http_error(503), acquire=True, phase="subtitle").code == "network"
    # Extraction keeps its existing handling.
    assert worker.classify(_http_error(403), acquire=True, phase="extract").code == "extractor_failed"
    assert worker.classify(_http_error(403), acquire=False).code == "extractor_failed"


def test_a_tag_may_quote_a_link_but_no_operand_reaches_the_network():
    tools = {"ffmpeg": "/opt/dp/tools/ffmpeg", "ffprobe": "/opt/dp/tools/ffprobe"}
    box = worker.Sandbox(("127.0.0.1", 9), tools, ("/w",), (), None)
    # Transfer 552's shape: HLS video + Opus audio + generated VTT into WebM,
    # with a description quoting links.
    components = [("/w/component-0.mp4", {"format_id": "616", "vcodec": "vp09.00.40.08", "acodec": "none"}),
                  ("/w/component-1.webm", {"format_id": "251", "vcodec": "none", "acodec": "opus"})]
    metadata = {"title": "Put them in the box", "artist": "Channel", "date": "2026-10-08",
                "description": "Full set: https://example.org/watch?v=1&t=2 -- also rtmp://live.example.org/x"}
    argv = worker.finalization_argv(tools, "webm", components, ("/w/subtitle.vtt", "en"), metadata,
                                    "/w/output.webm")
    box("subprocess.Popen", (None, argv, None, None))
    assert f"description={metadata['description']}" in argv  # written unchanged

    def refused(candidate, program=None):
        with pytest.raises(worker.SandboxRefusal):
            box("subprocess.Popen", (None, [program or candidate[0], *candidate[1:]], None, None))

    first_input = argv.index("file:/w/component-0.mp4")
    refused(argv[:first_input] + ["https://media.test/video.m3u8"] + argv[first_input + 1:])  # network -i
    refused(argv[:-1] + ["https://media.test/upload"])                                    # network output
    refused(argv[:-2] + ["-metadata", "https://media.test/x", *argv[-2:]])                # not a tag
    refused(argv[:-2] + ["-metadata:s:s:0", "rtmp://media.test/x", *argv[-2:]])
    refused(argv[:-2] + ["-i", "-metadata", "https://media.test/x", *argv[-2:]])
    # Only the finalizer's grammar is read this way.
    refused([tools["ffprobe"], "-metadata", "x=https://media.test/x"])


def test_the_failure_phase_is_bounded_context_never_identity(tmp_path):
    from services.transfer_trace import _exported, _Sanitizer
    from transfers.errors import NormalizedError
    from transfers.policy import failure_signature, recovery_action
    executor = MediaExecutor(str(tmp_path), str(tmp_path / "runtime"), _Repository().authorize_execution)
    handle = None
    record = {"state": "failed", "outcome": "source_refused", "detail": "HTTP Error 403: Forbidden",
              "context": {"phase": "component", "component": 1, "format_id": "251"}}
    error = executor._from_record(handle, record, None).error
    assert dict(error.context) == {"phase": "component", "component": 1, "format_id": "251"}
    bare = outcome_error("source_refused", Stage.EXECUTION, detail="HTTP Error 403: Forbidden")
    assert failure_signature(error) == failure_signature(bare) and recovery_action(error) == recovery_action(bare)
    # Durable: the stored encoding, its decoding and the trace export keep it.
    stored = json.dumps(error.as_dict(diagnostics=True))
    assert dict(NormalizedError.from_dict(json.loads(stored)).context) == dict(error.context)
    assert json.loads(_exported(_Sanitizer(), "normalized_error", stored))["context"] == dict(error.context)
    # Only the known phase vocabulary and bounded scalars ever cross.
    for context, expected in (({"phase": "finalize", "url": "https://media.test/x", "component": True},
                               {"phase": "finalize"}),
                              ({"phase": "somewhere", "component": 0}, {}), ("install", {})):
        failed = dict(record, context=context)
        assert dict(executor._from_record(handle, failed, None).error.context) == expected
    # A malformed record is read with the same allowlist: an identifier
    # outside it is dropped whole, never shortened into a valid-looking one.
    from integrations.media.outcomes import FORMAT_ID
    assert FORMAT_ID.pattern == worker._FORMAT_ID.pattern
    for format_id in ("https://media.test/x", "x" * 65, "251 x", "../251", "-251", "", 251, None):
        failed = dict(record, context={"phase": "component", "component": 0, "format_id": format_id})
        assert dict(executor._from_record(handle, failed, None).error.context) == {
            "phase": "component", "component": 0}, format_id
    longest = "f" * 64
    failed = dict(record, context={"phase": "component", "component": 0, "format_id": longest})
    assert executor._from_record(handle, failed, None).error.context["format_id"] == longest
    phase = worker._Phase()
    for format_id, expected in (("251", "251"), ("hls-1080p+dash", "hls-1080p+dash"),
                                ("https://media.test/x", ""), ("x" * 65, ""), (None, "")):
        phase.enter("component", 0, format_id)
        assert phase.context().get("format_id", "") == expected
    phase.enter("finalize")
    assert phase.context() == {"phase": "finalize"}


def test_badges_name_one_media_download_in_hot_rose_and_leave_others_as_they_were():
    from api.routes import _public_transfer_presentation
    media = _public_transfer_presentation({"id": 1, "origin_provider_id": "media", "current_provider_id": "media"},
                                          catalog)
    assert (media["origin_provider_name"], media["origin_provider_theme"]) == ("Media Download", "hot-rose")
    plain = _public_transfer_presentation({"id": 2, "origin_provider_id": "general_http"}, catalog)
    assert plain["origin_provider_name"] == "HTTP(S)" and "origin_provider_theme" not in plain


# -- presentation -------------------------------------------------------------------------

STATIC = __import__("pathlib").Path(__file__).resolve().parents[2] / "frontend" / "static"


def _function(text: str, name: str) -> str:
    body = text[text.index(f"function {name}("):]
    return body[:body.index("\n  function ", 1)]


def test_the_settings_and_badge_surfaces_use_the_one_shared_grammar():
    import re
    import typing

    page = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
    chips = (STATIC / "ui-settings-card-icons.css").read_text(encoding="utf-8")
    badges = (STATIC / "ui-transfer-contract.css").read_text(encoding="utf-8")
    glyph = (STATIC / "icons" / "lucide" / "monitor-down.svg").read_text(encoding="utf-8")

    # Services -> Network Sources and Downloads -> Transfer Method Settings: the
    # one protocol chip, the exact Lucide MonitorDown glyph, Hot Rose declared
    # once as that chip's canonical colour.
    assert "    media: 'monitor-down'," in page
    assert "Lucide monitor-down @ 23f9abc4ed0146cffededd3d7f94c1018bfdf693" in glyph and 'stroke="#EF137F"' in glyph
    assert re.search(r"\[data-protocol='media'\] \{\s*--dp-protocol-color: #EF137F;\s*\}", chips)
    assert "    media: ['Downloads supported media from', 'compatible web pages and media sites.']," in page
    panel = _function(page, "downloadsPanel")
    cards = re.findall(r"executorTuningCard\('(\w+)', '([^']+)'", panel)
    assert cards == [("direct", "Network Sources"), ("rsync", "rsync"), ("webdav", "WebDAV"), ("usenet", "Usenet"),
                     ("media", "Media Downloads")]
    assert "'Downloads supported web-hosted media in its native form, without transcoding.', mediaTuning(s),\n" \
           "        'media')" in panel

    # Its one tuning: Target Resolution, exactly the backend's values, in the
    # integration's own namespace; no raw yt-dlp control.
    tuning = _function(page, "mediaTuning")
    assert "selectField('media_target_resolution', 'Target Resolution'" in tuning
    assert "preferred_subtitle_language" not in tuning and "yt-dlp" not in tuning
    choices = re.findall(r"\['(\w+)', '([^']+)'\]", page.split("const TARGET_RESOLUTIONS = Object.freeze([", 1)[1]
                         .split("]);", 1)[0])
    from integrations.media.definition import TargetResolution
    assert [value for value, _label in choices] == list(typing.get_args(TargetResolution)) == list(
        planning.TARGET_RESOLUTIONS)
    assert choices[0] == ("best", "Best Available")
    assert "media_target_resolution: {scope: 'integration:media', option: 'target_resolution'}," in page

    # The global Preferred Subtitle Language lives with the global Downloads
    # behaviour, in the settings document -- never in the Media Downloads card.
    advanced = _function(page, "downloadBehaviorAdvanced")
    assert "input('preferred_subtitle_language', 'Preferred Subtitle Language'" in advanced
    assert ("preferred_subtitle_language: {scope: 'settings-document', option: 'preferred_subtitle_language'},"
            in page)

    # The provider badge: the shared chip, whose one per-badge datum is its
    # accent; Hot Rose changes only that, and only for the declared theme.
    base = badges[badges.index(".dp-provider-chip {"):]
    base = base[:base.index("}")]
    assert "--dp-provider-accent: var(--dp-accent-purple-bright, var(--accent));" in base
    assert "color: var(--dp-provider-accent);" in base
    assert re.search(r'\.dp-provider-chip\[data-provider-theme="hot-rose"\] \{\s*--dp-provider-accent: #EF137F;\s*\}',
                     badges)
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    chip = app[app.index("function providerChip("):]
    chip = chip[:chip.index("\nfunction ", 1)]
    assert "data-provider-theme" in chip and "dp-provider-chip" in chip
    # No renderer names this provider to rename or recolour it.
    for renderer in ("app.js", "ui-root-provider.js", "ui-downloads.js", "ui-dashboard-transfer-presentation.js"):
        text = (STATIC / renderer).read_text(encoding="utf-8")
        for literal in ("'media'", '"media"', "Media Download", "hot-rose", "#EF137F", "yt_dlp"):
            assert literal not in text, (renderer, literal)


# -- unified progress: the attempt's total is known as soon as yt-dlp knows it ---------

class _PlannedYoutubeDL:
    """yt-dlp's surface ``acquire`` drives, with two planned components: a
    video whose exact size is known and an audio track offered only with an
    estimate (``filesize_approx`` is never a total)."""

    info = {"extractor_key": "Youtube", "id": "abc", "title": "Clip", "requested_formats": [
        {"format_id": "137", "ext": "mp4", "protocol": "https", "url": "https://v.example/137", "filesize": 3000},
        {"format_id": "140", "ext": "m4a", "protocol": "https", "url": "https://v.example/140",
         "filesize_approx": 1000}]}
    fetched = (3000, 1000)

    def __init__(self, params):
        self.hooks = params["progress_hooks"]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, _url, download=False):
        return dict(self.info)

    def urlopen(self, url):
        import io
        self.opened = url
        return io.BytesIO(b"WEBVTT\n\n")

    def dl(self, path, item):
        size = self.fetched[["137", "140"].index(item["format_id"])]
        total = item.get("filesize")
        for done, status in ((size // 2, "downloading"), (size, "finished")):
            for hook in self.hooks:
                hook({"status": status, "downloaded_bytes": done, "total_bytes": total})
        with open(path, "wb") as handle:
            handle.write(b"x" * size)


def test_the_worker_reports_every_planned_components_exact_size_before_fetching(tmp_path, monkeypatch):
    import yt_dlp
    events = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _PlannedYoutubeDL)
    monkeypatch.setattr(worker, "_emit", events.append)
    monkeypatch.setattr(worker, "PROGRESS_INTERVAL", 0)
    monkeypatch.setattr(worker, "_run", lambda argv: open(argv[-1].removeprefix("file:"), "wb").write(b"muxed"))
    spec = {"url": "https://v.example/watch", "proxy": "http://127.0.0.1:9", "workspace": str(tmp_path / "w"),
            "target": str(tmp_path / "Clip.mp4"), "tools": {"ffmpeg": "/bin/true"},
            "plan": {"container": "mp4", "formats": ["137", "140"], "extractor": "Youtube", "id": "abc",
                     "subtitle": None}}
    assert worker.acquire(spec, worker._Phase()) == 0
    progress = [event for event in events if event.get("event") == "progress"]
    # Before any byte: the exact size where yt-dlp has one, and no guess where it has an estimate.
    assert progress[:2] == [
        {"event": "progress", "component": 0, "downloaded": 0, "total": 3000, "units": None, "unit_total": None,
         "finished": False},
        {"event": "progress", "component": 1, "downloaded": 0, "total": None, "units": None, "unit_total": None,
         "finished": False}]
    assert events.index(progress[1]) < events.index(progress[2])
    assert events[-1] == {"event": "completed", "bytes": len(b"muxed")}


def _media_run(components):
    from types import SimpleNamespace
    from executors.media.executor import _Run
    reader = asyncio.StreamReader()
    run = _Run(SimpleNamespace(process=SimpleNamespace(stdout=reader)), None, None, components, 0.0)
    return run, reader


async def _observed(run, reader, *events):
    from transfers.models import ExecutionHandle
    executor = MediaExecutor.__new__(MediaExecutor)
    for event in events:
        reader.feed_data((json.dumps(event) + "\n").encode())
    follower = asyncio.ensure_future(executor._follow(run))
    await asyncio.sleep(0.01)
    follower.cancel()
    return executor._running(ExecutionHandle("media", "a", {}), run)


def _progress(component, downloaded, total):
    return {"event": "progress", "component": component, "downloaded": downloaded, "total": total}


@pytest.mark.asyncio
async def test_a_multi_component_attempt_has_a_total_from_its_first_byte():
    run, reader = _media_run(2)
    first = await _observed(run, reader, _progress(0, 0, 3000), _progress(1, 0, 1000))
    assert (first.progress.total_bytes, first.progress.completed_bytes) == (4000, 0)     # known 0, not unknown
    moving = await _observed(run, reader, _progress(0, 1500, 3000))
    assert (moving.progress.total_bytes, moving.progress.completed_bytes) == (4000, 1500)
    assert moving.progress.percentage == pytest.approx(37.5)                           # visibly advances
    # A later event that omits the total does not withdraw a known one.
    later = await _observed(run, reader, _progress(0, 3000, None), _progress(1, 400, None))
    assert (later.progress.total_bytes, later.progress.completed_bytes) == (4000, 3400)
    assert later.activity.network_active


@pytest.mark.asyncio
async def test_an_estimated_component_keeps_the_total_unknown_until_it_reports_its_own():
    run, reader = _media_run(2)
    planned = await _observed(run, reader, _progress(0, 0, 3000), _progress(1, 0, None))
    assert planned.progress.total_bytes == 0                                           # unknown: no fabricated %
    video = await _observed(run, reader, _progress(0, 3000, 3000))
    assert video.progress.total_bytes == 0 and video.progress.completed_bytes == 3000
    audio = await _observed(run, reader, _progress(1, 10, 900))
    assert (audio.progress.total_bytes, audio.progress.completed_bytes) == (3900, 3010)


@pytest.mark.asyncio
async def test_finalization_is_local_work_with_no_acquisition_rate():
    from transfers._engine_base import TransferEngine
    run, reader = _media_run(1)
    await _observed(run, reader, _progress(0, 0, 10))
    finalizing = await _observed(run, reader, _progress(0, 10, 10), {"event": "phase", "phase": "finalize"})
    assert (finalizing.progress.total_bytes, finalizing.progress.completed_bytes) == (10, 10)
    assert not finalizing.activity.network_active
    assert TransferEngine._acquired_bytes(finalizing) is None                          # no speed while muxing


# -- segmented media (HLS): what yt-dlp actually reports, through the real path -------

@pytest.mark.asyncio
async def test_a_segmented_download_reports_exact_bytes_but_no_total_until_it_finishes(tmp_path, monkeypatch):
    """Real yt-dlp HLS (``m3u8_native``) over a local playlist of uneven
    segments: its hooks carry exact cumulative bytes and an exact fragment
    count, but ``total_bytes`` only on the final event (``total_bytes_estimate``
    is an average-fragment extrapolation and is never a total). Through the
    worker's hook and the executor's reader, the attempt therefore has moving
    acquired bytes (speed) and no percentage until the component is done."""
    import http.server
    import threading
    from yt_dlp import YoutubeDL
    segments = [b"a" * 1000, b"b" * 9000, b"c" * 3000, b"d" * 500]
    playlist = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
                + "".join(f"#EXTINF:2.0,\n{index}.ts\n" for index in range(len(segments))) + "#EXT-X-ENDLIST\n")

    class Origin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.strip("/")
            body = playlist.encode() if name == "media.m3u8" else segments[int(name.split(".")[0])]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    events = []
    monkeypatch.setattr(worker, "_emit", events.append)
    monkeypatch.setattr(worker, "PROGRESS_INTERVAL", 0)
    hook = worker._Progress()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/media.m3u8"
        with YoutubeDL({"quiet": True, "noprogress": True, "progress_hooks": [hook]}) as ydl:
            ydl.dl(str(tmp_path / "component-0.ts"), {"id": "x", "url": url, "protocol": "m3u8_native",
                                                      "ext": "mp4", "http_headers": {}})
    finally:
        server.shutdown()
    progress = [event for event in events if event["event"] == "progress"]
    downloaded = [event["downloaded"] for event in progress]
    assert downloaded == sorted(downloaded) and downloaded[-1] == sum(map(len, segments))
    assert all(event["total"] is None for event in progress[:-1])        # no total while it downloads
    assert progress[-1]["total"] == sum(map(len, segments))              # only once it has finished
    # Its units are COMPLETED segments of the exact segment count: never ahead
    # of the segments its bytes have covered, never backwards, all at the end.
    ends = [sum(map(len, segments[:count])) for count in range(len(segments) + 1)]
    units = [event["units"] for event in progress]
    assert all(event["unit_total"] == len(segments) for event in progress)
    assert units == sorted(units) and units[-1] == len(segments)
    assert all(ends[unit] <= event["downloaded"] for unit, event in zip(units, progress))
    run, reader = _media_run(1)
    seen = [await _observed(run, reader, {**event, "event": "progress"}) for event in progress]
    assert all(item.progress.total_bytes == 0 for item in seen[:-1])     # no byte total: never a byte percentage
    assert [item.progress.completed_bytes for item in seen] == downloaded  # yet the bytes move (speed)
    assert [(item.progress.completed_units, item.progress.total_units) for item in seen[:-1]] == [
        (unit, len(segments)) for unit in units[:-1]]                    # the part percentage advances
    # Finished, its exact byte total is known: the one scope is bytes, complete.
    assert (seen[-1].progress.total_bytes, seen[-1].progress.completed_bytes) == (13500, 13500)


def _parts(component, downloaded=0, total=None, units=None, unit_total=None):
    return {"event": "progress", "component": component, "downloaded": downloaded, "total": total,
            "units": units, "unit_total": unit_total}


@pytest.mark.asyncio
async def test_mixed_byte_and_segment_components_have_no_aggregate_percentage():
    """Exact-size video beside segmented audio: no coherent scope, so no
    percentage at any boundary -- only moving bytes."""
    run, reader = _media_run(2)
    for events in ([_parts(0, 0, 3000), _parts(1)], [_parts(0, 3000, 3000)], [_parts(1, 100, None, 1, 4)],
                   [_parts(1, 400, None, 4, 4)]):
        seen = await _observed(run, reader, *events)
        assert seen.progress.total_bytes == 0 and seen.progress.total_units is None
    assert seen.progress.completed_bytes == 3400


@pytest.mark.asyncio
async def test_a_subtitle_is_a_planned_part_of_either_coherent_scope():
    bytes_run, bytes_reader = _media_run(3)                            # video, audio, subtitle
    seen = await _observed(bytes_run, bytes_reader, _parts(2, 50, 50, 1, 1), _parts(0, 0, 3000), _parts(1, 0, 1000))
    assert (seen.progress.total_bytes, seen.progress.completed_bytes) == (4050, 50)
    seen = await _observed(bytes_run, bytes_reader, _parts(0, 3000, 3000))
    assert seen.progress.percentage == pytest.approx(3050 / 4050 * 100)  # no reset at the boundary
    parts_run, parts_reader = _media_run(2)                            # one segmented stream, subtitle
    seen = await _observed(parts_run, parts_reader, _parts(1, 50, 50, 1, 1), _parts(0, 0, None, 0, 9))
    assert (seen.progress.completed_units, seen.progress.total_units) == (1, 10)
    seen = await _observed(parts_run, parts_reader, _parts(0, 900, None, 4, 9))
    assert (seen.progress.total_bytes, seen.progress.completed_units, seen.progress.total_units) == (0, 5, 10)


@pytest.mark.asyncio
async def test_a_stream_that_has_not_started_keeps_the_part_scope_unknown():
    run, reader = _media_run(2)                                        # two segmented streams
    first = await _observed(run, reader, _parts(0, 10, None, 1, 8), _parts(1))
    assert first.progress.total_units is None                          # the audio's count is not known yet
    later = await _observed(run, reader, _parts(0, 80, None, 8, 8), _parts(1, 5, None, 0, 2))
    assert (later.progress.completed_units, later.progress.total_units) == (8, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [_parts(0, 30, None, 3, 9), _parts(0, 30, None, 1, 8), _parts(0, 30, None, 9, 8)])
async def test_changing_backward_or_overrunning_units_are_never_trusted_again(bad):
    run, reader = _media_run(1)
    seen = await _observed(run, reader, _parts(0, 20, None, 2, 8), bad, _parts(0, 40, None, 4, 8))
    assert seen.progress.completed_units is None and seen.progress.total_units is None
    assert seen.progress.completed_bytes == 40


def test_the_planned_subtitle_is_fetched_first_as_one_complete_part(tmp_path, monkeypatch):
    import yt_dlp

    class Subtitled(_PlannedYoutubeDL):
        info = {**_PlannedYoutubeDL.info,
                "subtitles": {"en": [{"ext": "vtt", "url": "https://v.example/en.vtt"}]}}
    events = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Subtitled)
    monkeypatch.setattr(worker, "_emit", events.append)
    monkeypatch.setattr(worker, "PROGRESS_INTERVAL", 0)
    monkeypatch.setattr(worker, "_run", lambda argv: open(argv[-1].removeprefix("file:"), "wb").write(b"muxed"))
    spec = {"url": "https://v.example/watch", "proxy": "http://127.0.0.1:9", "workspace": str(tmp_path / "w"),
            "target": str(tmp_path / "Clip.mkv"), "tools": {"ffmpeg": "/bin/true"},
            "plan": {"container": "mkv", "formats": ["137", "140"], "extractor": "Youtube", "id": "abc",
                     "subtitle": {"kind": "authored", "language": "en", "ext": "vtt"}}}
    assert worker.acquire(spec, worker._Phase()) == 0
    progress = [event for event in events if event.get("event") == "progress"]
    size = len(b"WEBVTT\n\n")
    assert progress[0] == {"event": "progress", "component": 2, "downloaded": size, "total": size,
                           "units": 1, "unit_total": 1, "finished": True}
    assert [event["component"] for event in progress[1:3]] == [0, 1]    # then every stream's plan
    assert all(event["downloaded"] == 0 for event in progress[1:3])     # before any stream byte


@pytest.mark.asyncio
async def test_only_planned_components_contribute_to_the_denominator():
    """The scope is exactly the plan: its streams plus a planned subtitle
    (``_Run.components``); an event for any other index counts for nothing."""
    run, reader = _media_run(1)
    seen = await _observed(run, reader, _parts(0, 10, 100), _parts(1, 50, 50, 1, 1), _parts(7, 9, 9))
    assert (seen.progress.total_bytes, seen.progress.completed_bytes) == (100, 10)
    assert seen.progress.total_units is None
