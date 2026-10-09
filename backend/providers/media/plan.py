"""Media Downloads acquisition planning: pure, provider-owned, I/O-free.

Everything here turns facts about a medium into the one plan its executor
carries out, before core commits the final file's name:

* which explicit installed yt-dlp extractor recognizes an address (in
  yt-dlp's own dispatch order; ``GenericIE`` is never a claimant);
* the native selection DebridPulse asks yt-dlp for -- its own format selector
  and sorter express the Target Resolution rule exactly (``res:<target>``
  prefers the largest resolution at or below the target, and only when there
  is none the smallest above it), limited to HTTP(S)-carried transports;
* the Video Preferences: a codec family and a relative bitrate rank among the
  video formats the site already offers (``quality_formats``), never an
  encoding target;
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
# Operator-facing Video Quality values, highest offered bitrate first.
VIDEO_QUALITIES = ("high", "normal", "low")
# Operator-facing Preferred Video Codec values; "auto" is yt-dlp's own codec order.
VIDEO_CODECS = ("auto", "av1", "hevc", "h264")
# Codec families recognized from yt-dlp's ``vcodec`` metadata alone (an RFC 6381
# codecs string or a plain codec name) -- never from an extension, container or
# format id. Anything else is unknown: never matched by a preference.
_CODEC_FAMILIES = (("av1", ("av01", "av1")), ("hevc", ("hvc1", "hev1", "h265", "hevc")),
                   ("h264", ("avc1", "avc3", "h264")), ("vp9", ("vp09", "vp9")))
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


def _none(codec) -> bool:
    return str(codec or "none") == "none"


def codec_family(vcodec) -> str | None:
    """The codec family a ``vcodec`` names, or ``None`` when it is unknown."""
    name = str(vcodec or "").strip().casefold()
    for family, prefixes in _CODEC_FAMILIES:
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            return family
    return None


def _bitrate(item) -> bool:
    rate = item.get("tbr")
    return isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0


def quality_formats(formats: list[dict], offered: list[dict], quality: str, subtitle: dict | None,
                    codec: str = "auto") -> list[dict]:
    """The native selection with its video chosen by the Video Preferences.

    Resolution and compatibility first: a candidate is an offered format of
    the native video's shape (video-only, or video with audio) at its height
    (the Target Resolution tier never changes), over a guarded transport,
    without DRM, that still gives a valid lossless container and subtitle plan
    with the native audio. Then the codec family -- ``auto`` keeps the native
    pick's own family (and its container extension); an explicit codec names
    one, recognized from ``vcodec`` alone. Then the bitrate rank WITHIN that
    family only, never across families: by provider-supplied ``tbr``, highest
    first, ties in yt-dlp's own preference order; High is rank 1, Normal rank
    ``ceil(n / 2)`` (the upper middle), Low rank ``n``.

    Auto + High is the native selection itself. Auto + Normal/Low with fewer
    than two rankable candidates keeps it. An explicit codec with two or more
    rankable members is ranked; with exactly one eligible member takes it;
    otherwise (absent, unknown, or several it cannot rank) keeps the native
    selection. Only the video changes; audio and subtitle stay as selected."""
    if codec not in VIDEO_CODECS:
        codec = "auto"
    if quality not in VIDEO_QUALITIES:
        quality = "high"
    if codec == "auto" and quality == "high":
        return formats
    videos = [index for index, item in enumerate(formats) if not _none(item.get("vcodec"))]
    if len(videos) != 1 or not offered:
        return formats
    position = videos[0]
    selected = formats[position]
    family = codec_family(selected.get("vcodec")) if codec == "auto" else codec
    if family is None:
        return formats

    def eligible(item) -> bool:
        if (_none(item.get("vcodec")) or _none(item.get("acodec")) != _none(selected.get("acodec"))
                or not isinstance(item.get("height"), int) or item.get("height") != selected.get("height")
                or item.get("drm") or str(item.get("protocol") or "") not in NATIVE_PROTOCOLS
                or codec_family(item.get("vcodec")) != family
                or (codec == "auto" and item.get("ext") != selected.get("ext"))):
            return False
        try:
            container_plan(formats[:position] + [item] + formats[position + 1:], subtitle)
        except ValueError:
            return False
        return True

    members = [(index, item) for index, item in enumerate(offered) if eligible(item)]
    ranked = sorted(((index, item) for index, item in members if _bitrate(item)),
                    key=lambda pair: (-pair[1]["tbr"], -pair[0]))
    if len(ranked) >= 2:
        choice = ranked[{"normal": (len(ranked) + 1) // 2, "low": len(ranked)}.get(quality, 1) - 1][1]
    elif codec != "auto" and len(members) == 1:
        choice = members[0][1]
    else:
        return formats
    if choice.get("format_id") == selected.get("format_id"):
        return formats
    chosen = {key: choice.get(key) for key in ("format_id", "ext", "vcodec", "acodec", "protocol", "height")}
    return formats[:position] + [chosen] + formats[position + 1:]


def plan(facts: dict, *, url: str, target: str, subtitle_language: str, video_quality: str = "high",
         video_codec: str = "auto") -> dict:
    """The durable acquisition plan for one medium (candidate context): only
    stable, non-secret facts -- never a media, manifest or subtitle URL."""
    formats = list(facts.get("formats") or [])
    if not formats or any(not item.get("format_id") for item in formats):
        raise ValueError("no_usable_formats")
    if any(str(item.get("protocol") or "") not in NATIVE_PROTOCOLS for item in formats):
        raise ValueError("transport_unsupported")
    subtitle_choice = choose_subtitle(facts, subtitle_language)
    try:
        formats = quality_formats(formats, list(facts.get("offered") or []), video_quality, subtitle_choice,
                                  video_codec)
    except ValueError:
        pass                                       # the native selection's own plan decides below
    container, subtitle = container_plan(formats, subtitle_choice)
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
        "video_quality": video_quality if video_quality in VIDEO_QUALITIES else "high",
        "video_codec": video_codec if video_codec in VIDEO_CODECS else "auto",
        "subtitle_language": subtitle_language,
        "selected_height": max(heights) if heights else None,
    }
