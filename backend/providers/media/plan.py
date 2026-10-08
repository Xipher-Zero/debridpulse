"""Media Downloads acquisition planning: pure, provider-owned, I/O-free.

Everything here turns facts about a medium into the one plan its executor
carries out, before core commits the final file's name:

* which explicit installed yt-dlp extractor recognizes an address (in
  yt-dlp's own dispatch order; ``GenericIE`` is never a claimant);
* the native selection DebridPulse asks yt-dlp for -- its own format selector
  and sorter express the Target Resolution rule exactly (``res:<target>``
  prefers the largest resolution at or below the target, and only when there
  is none the smallest above it), limited to HTTP(S)-carried transports;
* the preferred-language subtitle: authored, else generated, else none --
  never every language;
* the final container: the native one whenever it carries every selected
  stream and the subtitle without conversion, otherwise MKV; a chosen
  subtitle no container carries unchanged fails the plan, never vanishes.
"""
from __future__ import annotations

import functools
import re

from transfers.filesystem import safe_name

# Operator-facing Target Resolution values; "best" is no target at all.
TARGET_RESOLUTIONS = ("best", "2160", "1440", "1080", "720", "480", "360")
# Transports yt-dlp's native downloaders carry over the guarded HTTP(S) route.
NATIVE_PROTOCOLS = frozenset({"http", "https", "m3u8_native", "http_dash_segments",
                              "http_dash_segments_generator"})
_PROTOCOL_FILTER = "[protocol~='^(https?|m3u8_native|http_dash_segments(_generator)?)$']"
# Containers the executor rewrites losslessly (``-c copy``) to embed metadata
# and a subtitle; a single native file in any other container is kept as is.
REWRITABLE_CONTAINERS = frozenset({"mp4", "m4a", "m4v", "mov", "webm", "mkv", "mka", "mp3", "ogg", "opus",
                                   "flac"})
# Subtitle formats each container carries natively (no conversion). MP4's only
# text subtitle is mov_text, which no source offers, so MP4 carries none.
_CARRIES = {"webm": ("vtt",), "mkv": ("srt", "ass", "ssa", "vtt"), "mka": ("srt", "ass", "ssa", "vtt")}
MKV = "mkv"
# Bounded complete collections: a larger one is refused, never truncated.
COLLECTION_BOUND = 100
_MAX_STEM = 180


@functools.lru_cache(maxsize=1)
def _extractor_classes() -> tuple:
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except ImportError:
        return ()
    # yt-dlp's own dispatch order: the first suitable class handles a URL.
    return tuple(gen_extractor_classes())


@functools.lru_cache(maxsize=4096)
def explicit_extractor(address: str) -> str | None:
    """The key of the explicit extractor yt-dlp would dispatch ``address``
    to, or ``None`` when only ``GenericIE`` (or nothing) would take it.
    Pure regex matching over the installed lazy extractor registry."""
    for extractor in _extractor_classes():
        try:
            suitable = extractor.suitable(address)
        except Exception:  # noqa: BLE001 -- an extractor that cannot judge does not match
            suitable = False
        if suitable:
            key = extractor.ie_key()
            return None if key == "Generic" else key
    return None


def selection(target: str) -> dict:
    """yt-dlp's native selection for ``target``: best video with best audio
    (or the best single format), only over guarded transports, sorted by
    resolution against the target when there is one."""
    selector = f"bv*{_PROTOCOL_FILTER}+ba{_PROTOCOL_FILTER}/b{_PROTOCOL_FILTER}"
    return {"format": selector, "format_sort": [] if target == "best" else [f"res:{int(target)}"]}


def match_language(codes, preferred: str) -> str | None:
    """The track language for ``preferred``: an exact code, else a regional
    variant of it, else (for a regional preference) its primary language."""
    preferred = str(preferred or "").casefold()
    if not preferred:
        return None
    available = sorted(str(code) for code in codes if str(code))
    for code in available:
        if code.casefold() == preferred:
            return code
    for code in available:
        if code.casefold().startswith(preferred + "-"):
            return code
    primary = preferred.split("-", 1)[0]
    if primary != preferred:
        for code in available:
            if code.casefold() == primary:
                return code
    return None


def choose_subtitle(facts: dict, preferred: str) -> dict | None:
    """Authored subtitle in the preferred language, else the generated one in
    the same language, else none."""
    for kind, table in (("authored", facts.get("subtitles") or {}), ("generated",
                                                                      facts.get("automatic_captions") or {})):
        code = match_language(table.keys(), preferred)
        if code is not None:
            return {"language": code, "kind": kind, "exts": tuple(table[code])}
    return None


def native_container(formats: list[dict]) -> str:
    """The container the selected native streams naturally share: a single
    format's own, or yt-dlp's own merge compatibility answer."""
    if len(formats) == 1:
        return str(formats[0].get("ext") or "").casefold()
    from yt_dlp.utils import get_compatible_ext
    video = [item for item in formats if str(item.get("vcodec") or "none") != "none"]
    audio = [item for item in formats if item not in video]
    return get_compatible_ext(
        vcodecs=[str(item.get("vcodec") or "") for item in video],
        acodecs=[str(item.get("acodec") or "") for item in audio],
        vexts=[str(item.get("ext") or "") for item in video],
        aexts=[str(item.get("ext") or "") for item in audio]).casefold()


def container_plan(formats: list[dict], subtitle: dict | None) -> tuple[str, dict | None]:
    """``(final container, subtitle with its one chosen format | None)``.

    ``subtitle`` is the chosen preferred-language track (``choose_subtitle``);
    ``None`` means no such track exists, and then nothing is embedded. The
    native container is kept when it carries every selected stream and that
    track unchanged; otherwise the container is MKV (Matroska). A chosen
    track that no container can carry without conversion is never dropped and
    never converted: planning fails (``subtitle_unembeddable``)."""
    native = native_container(formats)
    if subtitle is not None:
        exts = subtitle["exts"]
        # A container this integration cannot rewrite carries no subtitle.
        for container in ((native, MKV) if native in REWRITABLE_CONTAINERS else (MKV,)):
            carried = next((ext for ext in _CARRIES.get(container, ()) if ext in exts), None)
            if carried is not None:
                return container, {"language": subtitle["language"], "kind": subtitle["kind"], "ext": carried}
        raise ValueError("subtitle_unembeddable")
    if len(formats) > 1 and native not in REWRITABLE_CONTAINERS:
        return MKV, None
    return native, None


def file_name(title: str, media_id: str, container: str) -> str:
    stem = safe_name(f"{title or media_id} [{media_id}]" if media_id else (title or "media"))[:_MAX_STEM].strip()
    return f"{stem}.{container}" if container else stem


_ID = re.compile(r"[^\w.-]+")


def plan(facts: dict, *, url: str, target: str, subtitle_language: str) -> dict:
    """The durable acquisition plan for one medium (candidate context): only
    stable, non-secret facts -- never a media, manifest or subtitle URL."""
    formats = list(facts.get("formats") or [])
    if not formats or any(not item.get("format_id") for item in formats):
        raise ValueError("no_usable_formats")
    if any(str(item.get("protocol") or "") not in NATIVE_PROTOCOLS for item in formats):
        raise ValueError("transport_unsupported")
    container, subtitle = container_plan(formats, choose_subtitle(facts, subtitle_language))
    if not container or not _ID.sub("", container) == container:
        raise ValueError("no_usable_formats")
    heights = [item.get("height") for item in formats if isinstance(item.get("height"), int)]
    return {
        "v": 1,
        "url": url,
        "extractor": str(facts.get("extractor") or ""),
        "id": str(facts.get("id") or ""),
        "formats": [str(item["format_id"]) for item in formats],
        "container": container,
        "subtitle": subtitle,
        # Provenance: what was asked for, and what the native selection gave.
        "target_resolution": target,
        "subtitle_language": subtitle_language,
        "selected_height": max(heights) if heights else None,
    }
