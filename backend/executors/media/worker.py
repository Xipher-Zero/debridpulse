"""One sandboxed Media Downloads acquisition worker: a DebridPulse-owned child.

Started only by ``executors.media.sandbox`` as ``python -I -B worker.py`` in its
own process group, with a minimal environment and an empty private home. It
imports nothing of DebridPulse -- only the standard library and the pinned
``yt_dlp`` -- and reads its whole instruction, the attempt-scoped egress route
included, as ONE JSON document from standard input (never argv or the
environment).

Before ``yt_dlp`` is imported this process installs a Python audit hook that is
its network and filesystem boundary for the rest of its life:

* a socket may connect only to the DebridPulse egress guard's loopback
  listener, and only the guard's numeric address may be resolved -- every
  destination, redirect and CDN host is reached through the guard, which
  resolves and judges it at that connection; unconnected datagrams and other
  name lookups are refused outright;
* a child process may be only one of the packaged local tools the instruction
  names -- each the network-confined wrapper ``executors.media.netless``, in
  which the kernel refuses every network socket -- never with a network
  address in its arguments, and the JavaScript runtime only in its sandboxed
  form (no remote modules, no permission grants). This hook governs only this
  process's own code; the kernel filter is what binds the native helpers;
* files may be written only inside the attempt workspace, plus the attempt's
  durable result record and its one authorized target.

yt-dlp is used for exactly two things: extraction (``mode: extract``, read-only
facts for the provider's plan) and the native download of the components the
plan names (``mode: acquire``). Its postprocessors are never run: the lossless
finalization (one ``-c copy`` remux, into Matroska when the plan chose MKV)
is this worker's own, into the container the plan fixed before materialization.
Plugins, configuration files, caches, cookies, netrc and remote components are
never loaded.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time

MAX_SPEC_BYTES = 1024 * 1024
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 16 * 1024
MAX_DETAIL_CHARS = 300
PROGRESS_INTERVAL = 0.5
# The only native transports a component may use: HTTP(S) carries plain
# files, HLS playlists and DASH manifests alike, and each is downloaded by
# yt-dlp's own native downloader through the guarded proxy.
NATIVE_PROTOCOLS = frozenset({"http", "https", "m3u8_native", "http_dash_segments",
                              "http_dash_segments_generator"})
# Containers this worker can rewrite losslessly with ``-c copy``, and the
# ffmpeg muxer for each. Anything else is installed exactly as downloaded.
MUXERS = {"mp4": "mp4", "m4a": "ipod", "m4v": "mp4", "mov": "mov", "webm": "webm", "mkv": "matroska",
          "mka": "matroska", "mp3": "mp3", "ogg": "ogg", "opus": "opus", "flac": "flac"}
MP4_FAMILY = frozenset({"mp4", "m4a", "m4v", "mov"})
EXIT_FAILED = 3
_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*:(?://|\?)\S*")
_NETWORK_ARGUMENT = re.compile(r"(?i)^[a-z][a-z0-9+.-]*:(//|[a-z0-9]{1,8}:)|://")
_LOCAL_FILE_ARGUMENT = re.compile(r"^file:(?!//)")
# ffmpeg's tag options and their one "key=value" operand: data written into the
# output, never opened. A token starting "key=" has no URL scheme for ffmpeg
# ("=" is not a scheme character), so even read as a file name it is local.
_FFMPEG_TAG_OPTION = re.compile(r"^-metadata(:[A-Za-z0-9:]+)?$")
_FFMPEG_TAG = re.compile(r"^[A-Za-z0-9_]+=")
# The one format-identifier allowlist; ``integrations.media.outcomes.FORMAT_ID``
# applies the same pattern again where the record is read.
_FORMAT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
# The phases in which a remote answer is about the planned media itself.
_MEDIA_FETCH_PHASES = frozenset({"component", "subtitle"})


def _detail(value) -> str:
    text = _URL.sub("<url>", " ".join(str(value or "").split()))
    return text[:MAX_DETAIL_CHARS]


class Failure(Exception):
    """A classified outcome: ``code`` is the shared vocabulary both halves of
    the integration translate (``integrations.media.outcomes``)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = _detail(detail)


# ── the boundary ──────────────────────────────────────────────────────────


class SandboxRefusal(PermissionError):
    """The audit hook's refusal of one operation, raised at that operation.
    A failure is a policy refusal only when this exception is in its own
    causal chain: a refusal yt-dlp caught and handled (an executable probe it
    then does without) is never the cause of a later, unrelated failure."""

    def __init__(self, event: str):
        super().__init__(f"DebridPulse sandbox refused {event}")
        self.event = event


class Sandbox:
    """The audit hook. Every refusal is raised as ``SandboxRefusal``, so a
    refused operation is reported as the policy refusal it was, never as a
    generic failure."""

    _FILE_EVENTS = frozenset({"os.remove", "os.rmdir", "os.mkdir", "shutil.rmtree", "os.truncate", "os.chmod",
                              "os.chown", "os.utime"})

    def __init__(self, guard: tuple[str, int], executables: dict[str, str], writable: tuple[str, ...],
                 files: tuple[str, ...], deno: str | None):
        self.guard = (str(guard[0]), int(guard[1]))
        self.executables = {os.path.realpath(path) for path in executables.values() if path}
        self.ffmpeg = os.path.realpath(executables["ffmpeg"]) if executables.get("ffmpeg") else None
        self.deno = os.path.realpath(deno) if deno else None
        self.writable = tuple(os.path.realpath(path) for path in writable)
        self.files = {os.path.realpath(path) for path in files}

    @staticmethod
    def _refuse(event: str):
        raise SandboxRefusal(event)

    def _inside(self, path) -> bool:
        if isinstance(path, int):
            return True
        try:
            resolved = os.path.realpath(os.fsdecode(path))
        except (TypeError, ValueError):
            return False
        return resolved in self.files or any(resolved == root or resolved.startswith(root + os.sep)
                                             for root in self.writable)

    def _program(self, executable, argv) -> None:
        name = os.fsdecode(executable) if executable is not None else os.fsdecode(argv[0])
        resolved = shutil.which(name) if os.sep not in name else name
        path = os.path.realpath(resolved) if resolved else ""
        arguments = [os.fsdecode(item) for item in (argv or [])[1:]]
        if path not in self.executables:
            self._refuse("subprocess")
        if path == self.deno:
            if "run" in arguments and ("--no-remote" not in arguments or any(
                    item == "-A" or item.startswith(("--allow", "--unsafely")) for item in arguments)):
                self._refuse("subprocess")
            return
        for index, item in enumerate(arguments):
            if (path == self.ffmpeg and index and _FFMPEG_TAG_OPTION.match(arguments[index - 1])
                    and _FFMPEG_TAG.match(item)):
                # A tag value (a description may well quote a link): ffmpeg
                # writes it into the output and never opens it.
                continue
            if _NETWORK_ARGUMENT.search(item) and not _LOCAL_FILE_ARGUMENT.match(item):
                self._refuse("subprocess")

    def __call__(self, event: str, args) -> None:
        if event == "socket.connect":
            # An address is checked whatever its family: only the guard's
            # loopback listener ever matches.
            address = args[1]
            if not (isinstance(address, tuple) and len(address) >= 2
                    and (str(address[0]), int(address[1])) == self.guard):
                self._refuse(event)
        elif event == "socket.getaddrinfo":
            host = args[0].decode("ascii", "replace") if isinstance(args[0], bytes) else str(args[0])
            if host != self.guard[0]:
                self._refuse(event)
        elif event in {"socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg"}:
            # Binding is not egress (a library's local IPv6 probe binds ::1);
            # every way of sending to a peer is checked or refused here.
            self._refuse(event)
        elif event == "subprocess.Popen":
            self._program(args[0], args[1])
        elif event == "os.posix_spawn":
            self._program(args[0], args[1])
        elif event in {"os.system", "os.exec", "os.spawn", "os.fork", "os.forkpty", "pty.spawn", "os.symlink",
                       "os.link"}:
            self._refuse(event)
        elif event == "open":
            path, mode, flags = args
            writes = (any(char in mode for char in "wax+") if isinstance(mode, str)
                      else bool(int(flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))
            if writes and path != os.devnull and not self._inside(path):
                self._refuse(event)
        elif event == "os.rename":
            if not (self._inside(args[0]) and self._inside(args[1])):
                self._refuse(event)
        elif event in self._FILE_EVENTS:
            if not self._inside(args[0]):
                self._refuse(event)


# ── yt-dlp ────────────────────────────────────────────────────────────────


class _Log:
    """yt-dlp's logger: bounded, URL-free diagnostics only."""

    def __init__(self):
        self.lines: list[str] = []

    def _keep(self, message):
        if len(self.lines) < 20:
            self.lines.append(_detail(message))

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        self._keep(message)

    def error(self, message):
        self._keep(message)


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _params(spec: dict, log: _Log, **extra) -> dict:
    tools = spec.get("tools") or {}
    params = {
        "quiet": True, "no_warnings": True, "noprogress": True, "no_color": True, "logger": log,
        # The attempt's one route: every request, redirect and fragment
        # reaches the network only through the guard.
        "proxy": spec["proxy"],
        "socket_timeout": float(spec.get("socket_timeout") or 30),
        # One connection per request, so every request is admitted on its own.
        "compat_opts": ["prefer-legacy-http-handler"],
        # Explicit extractors only: GenericIE is never a fallback.
        "allowed_extractors": ["default", "-generic"],
        "cachedir": False, "enable_file_urls": False, "usenetrc": False, "cookiefile": None,
        "remote_components": [],
        "js_runtimes": {"deno": {"path": tools["deno"]}} if tools.get("deno") else {},
        "ffmpeg_location": tools.get("ffmpeg") or None,
        "external_downloader": {"default": "native"},
        "check_formats": False, "updatetime": False, "overwrites": True, "continuedl": False,
        "writesubtitles": False, "writeautomaticsub": False, "writethumbnail": False, "writeinfojson": False,
        "writedescription": False, "writeannotations": False, "writecomments": False, "getcomments": False,
        "retries": 3, "fragment_retries": 3, "extractor_retries": 1, "concurrent_fragment_downloads": 1,
    }
    params.update(extra)
    return params


def _chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        info = getattr(exc, "exc_info", None)
        nested = info[1] if isinstance(info, tuple) and len(info) > 1 else None
        exc = nested or getattr(exc, "cause", None) or exc.__cause__ or exc.__context__


_AUTH = re.compile(r"(?i)sign in|log ?in|logged[- ]in|cookies|members?[- ]only|private video|video is private|"
                   r"premium|subscri|age[- ]restricted|confirm your age|authenticat|registered users|"
                   r"requires? (an )?account")
_LIVE = re.compile(r"(?i)\bis live\b|live event|premieres? in|will begin|stream is offline|not currently live|"
                   r"upcoming|scheduled")
_GONE = re.compile(r"(?i)unavailable|not available|removed|deleted|does not exist|no longer|terminated|"
                   r"not found|copyright|has been blocked")
_NO_FORMATS = re.compile(r"(?i)requested format is not available|no video formats|no formats found")


def classify(exc, *, acquire: bool, phase: str = "") -> Failure:
    """One yt-dlp failure, as the shared outcome vocabulary, read from that
    failure's own causal chain. ``phase`` is the attempt's phase when it
    failed (``_Phase``)."""
    if isinstance(exc, Failure):
        return exc
    from yt_dlp.networking.exceptions import HTTPError, ProxyError, TransportError
    from yt_dlp.utils import GeoRestrictedError, UnsupportedError
    message = str(exc)
    for item in _chain(exc):
        if isinstance(item, SandboxRefusal):
            code = ("egress_refused" if item.event.startswith("socket.")
                    else "transport_unsupported" if item.event == "subprocess" else "path_refused")
            return Failure(code, message)
        if isinstance(item, (ProxyError, TransportError)) and re.search(r"(?i)tunnel connection failed: 407",
                                                                          str(item)):
            return Failure("route_revoked", str(item))
        if isinstance(item, ProxyError) or (isinstance(item, TransportError) and re.search(
                r"(?i)tunnel connection failed: 403", str(item))):
            return Failure("egress_refused", str(item))
        if isinstance(item, HTTPError) and int(getattr(item, "status", 0) or 0) in {403, 407}:
            # A plain-HTTP request's proxy answer arrives as a response: only
            # the guard's own refusal says so (Proxy-Status), and only a proxy
            # ever answers 407.
            headers = getattr(getattr(item, "response", None), "headers", None) or {}
            if int(item.status) == 407:
                return Failure("route_revoked", str(item))
            if "debridpulse" in str(headers.get("Proxy-Status") or ""):
                return Failure("egress_refused", str(item))
            if int(item.status) == 403 and phase in _MEDIA_FETCH_PHASES:
                # The origin itself refused a planned component or subtitle
                # (an HTTPS refusal by the guard never arrives as a response).
                # Every attempt extracts afresh, so a later attempt asks with
                # newly issued addresses.
                return Failure("source_refused", str(item))
        if isinstance(item, GeoRestrictedError):
            return Failure("geo_restricted", str(item))
        if isinstance(item, UnsupportedError):
            return Failure("unsupported", str(item))
        if isinstance(item, HTTPError):
            status = int(getattr(item, "status", 0) or 0)
            if status == 429:
                return Failure("rate_limited", str(item))
            if status in {404, 410}:
                return Failure("unavailable", str(item))
            if status >= 500:
                return Failure("network", str(item))
        elif isinstance(item, TransportError):
            return Failure("network", str(item))
    if "No suitable extractor" in message:
        return Failure("unsupported", message)
    if _NO_FORMATS.search(message):
        return Failure("format_unavailable" if acquire else "no_usable_formats", message)
    if _AUTH.search(message):
        return Failure("auth_required", message)
    if _LIVE.search(message):
        return Failure("live_unsupported", message)
    if _GONE.search(message):
        return Failure("unavailable", message)
    return Failure("extractor_failed", message)


def _tracks(table) -> dict:
    result = {}
    for language, tracks in (table or {}).items():
        exts = sorted({str(item.get("ext")) for item in tracks or () if isinstance(item, dict) and item.get("ext")})
        if exts:
            result[str(language)] = exts
    return result


# Bounds the offered-format facts a plan may choose a Video Quality among.
MAX_OFFERED_FORMATS = 256


def _bitrate(value):
    """A provider-supplied bitrate (kbit/s), or ``None``: never estimated."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _offered(info: dict) -> list:
    """Every video format the site offers, in yt-dlp's own preference order
    (least preferred first), with only the facts a Video Quality choice reads:
    never an address, header or manifest."""
    formats = [item for item in info.get("formats") or () if isinstance(item, dict)
               and str(item.get("vcodec") or "none") != "none"]
    return [{
        "format_id": str(item.get("format_id") or ""),
        "ext": str(item.get("ext") or ""),
        "vcodec": str(item.get("vcodec") or ""),
        "acodec": str(item.get("acodec") or ""),
        "protocol": str(item.get("protocol") or ""),
        "height": item.get("height") if isinstance(item.get("height"), int) else None,
        "tbr": _bitrate(item.get("tbr")),
        "drm": bool(item.get("has_drm")),
    } for item in formats[-MAX_OFFERED_FORMATS:]]


def _facts(info: dict) -> dict:
    requested = info.get("requested_formats") or [info]
    return {
        "kind": "media",
        "extractor": str(info.get("extractor_key") or ""),
        "id": str(info.get("id") or ""),
        # The medium's own page: the one address that names it alone.
        "webpage_url": str(info.get("webpage_url") or ""),
        "title": str(info.get("title") or "")[:512],
        "live_status": str(info.get("live_status") or ("is_live" if info.get("is_live") else "")),
        "formats": [{
            "format_id": str(item.get("format_id") or ""),
            "ext": str(item.get("ext") or ""),
            "vcodec": str(item.get("vcodec") or ""),
            "acodec": str(item.get("acodec") or ""),
            "protocol": str(item.get("protocol") or ""),
            "height": item.get("height") if isinstance(item.get("height"), int) else None,
            "width": item.get("width") if isinstance(item.get("width"), int) else None,
        } for item in requested],
        "subtitles": _tracks(info.get("subtitles")),
        "automatic_captions": _tracks(info.get("automatic_captions")),
        "offered": _offered(info),
    }


def bounded_entries(info: dict, bound: int) -> tuple[list, bool, int | None]:
    """A collection's first ``bound`` entries in source order, whether the
    source holds more, and its entry count only where yt-dlp states one
    (``None``: unknown). The extraction asked for one entry more than the
    bound, so reaching it proves the excess without enumerating the rest; a
    stated count never contradicts the entries actually observed."""
    entries = list(info.get("entries") or [])
    total = info.get("playlist_count")
    if isinstance(total, bool) or not isinstance(total, int) or total < len(entries[:bound + 1]):
        total = None
    return entries[:bound], len(entries) > bound or (total is not None and total > bound), total


def extract(spec: dict) -> dict:
    """Read-only facts for the provider's plan: one medium (only that medium
    when ``single_item``), or a collection's bounded first entries -- whether
    the source holds more stated separately -- each member itself planned (or
    failed)."""
    from yt_dlp import YoutubeDL
    selection = spec.get("selection") or {}
    bound = int(spec.get("collection_bound") or 0)
    # One explicitly identified medium: yt-dlp's own single-item reading of an
    # address that also names its enclosing playlist -- never its expansion.
    single = spec.get("single_item") is True
    log = _Log()
    params = _params(spec, log, format=selection.get("format"), format_sort=list(selection.get("format_sort") or []),
                     extract_flat="in_playlist", playlistend=bound + 1, lazy_playlist=False, noplaylist=single)
    with YoutubeDL(params) as ydl:
        try:
            info = ydl.extract_info(spec["url"], download=False)
        except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
            raise classify(exc, acquire=False) from None
        if not isinstance(info, dict):
            raise Failure("extractor_failed", "no information")
        if info.get("_type") not in {"playlist", "multi_video"}:
            return _facts(info)
        if single:
            raise Failure("unsupported", "the item's address names only a collection")
        entries, truncated, total = bounded_entries(info, bound)
        members = []
        for entry in entries:
            if not isinstance(entry, dict):
                members.append({"outcome": "unavailable"})
                continue
            address = str(entry.get("url") or entry.get("webpage_url") or "")
            member = {"url": address, "id": str(entry.get("id") or ""), "title": str(entry.get("title") or "")[:512]}
            try:
                detail = ydl.extract_info(address, download=False)
                if not isinstance(detail, dict) or detail.get("_type") in {"playlist", "multi_video"}:
                    raise Failure("unsupported", "nested collection")
                member.update(_facts(detail))
            except Exception as exc:  # noqa: BLE001 -- the member's own outcome
                failure = classify(exc, acquire=False)
                if failure.code in {"egress_refused", "transport_unsupported", "network", "rate_limited"}:
                    raise failure from None
                member["outcome"] = failure.code
            members.append(member)
        return {"kind": "collection", "extractor": str(info.get("extractor_key") or ""),
                "id": str(info.get("id") or ""), "title": str(info.get("title") or "")[:512], "members": members,
                "truncated": truncated, "total": total}


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class _Progress:
    """yt-dlp progress, per component, emitted no more often than the interval.

    A segmented download's units are its COMPLETED fragments: yt-dlp's
    ``fragment_index`` counts the fragments already finished, out of the exact
    ``fragment_count``. Its ``total_bytes_estimate`` is never a total."""

    def __init__(self):
        self.component = 0
        self.last = 0.0
        self.unit_totals: dict[int, int] = {}

    def __call__(self, status: dict) -> None:
        now = time.monotonic()
        finished = status.get("status") == "finished"
        if not finished and now - self.last < PROGRESS_INTERVAL:
            return
        self.last = now
        total = status.get("total_bytes")
        index, count = _count(status.get("fragment_index")), _count(status.get("fragment_count"))
        if count and index is not None and index <= count:
            self.unit_totals[self.component] = count
        else:
            count = index = None
        if finished and self.component in self.unit_totals:
            count = index = self.unit_totals[self.component]          # every fragment is done
        _emit({"event": "progress", "component": self.component,
               "downloaded": int(status.get("downloaded_bytes") or 0),
               "total": int(total) if isinstance(total, (int, float)) and total > 0 else None,
               "units": index, "unit_total": count, "finished": finished})


class _Phase:
    """Where an acquisition attempt is: entered immediately before the
    operation it names, so a failure is recorded with the operation it ended.
    Bounded scalars only -- never an address, header or native payload."""

    def __init__(self):
        self.name = ""
        self.component: int | None = None
        self.format_id = ""

    def enter(self, name: str, component: int | None = None, format_id=None) -> None:
        self.name, self.component = name, component
        value = str(format_id or "")
        self.format_id = value if _FORMAT_ID.fullmatch(value) else ""

    def context(self) -> dict:
        context = {"phase": self.name} if self.name else {}
        if self.component is not None:
            context["component"] = self.component
        if self.format_id:
            context["format_id"] = self.format_id
        return context


def _run(argv: list[str]) -> None:
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, check=False)
    except SandboxRefusal:
        raise  # a policy refusal, classified as one -- never a missing runtime
    except OSError as exc:
        raise Failure("runtime_unavailable", str(exc)) from None
    if completed.returncode != 0:
        raise Failure("finalization_failed", completed.stderr.decode("utf-8", "replace")[-MAX_DETAIL_CHARS:])


def _metadata(info: dict) -> dict:
    date = str(info.get("upload_date") or "")
    return {key: value for key, value in (
        ("title", str(info.get("title") or "")),
        ("description", str(info.get("description") or "")[:MAX_DESCRIPTION_CHARS]),
        ("artist", str(info.get("uploader") or info.get("channel") or "")),
        ("date", f"{date[:4]}-{date[4:6]}-{date[6:8]}" if re.fullmatch(r"\d{8}", date) else ""),
    ) if value}


def finalization_argv(tools: dict, container: str, components: list[tuple[str, dict]], subtitle: tuple | None,
                      metadata: dict, output: str) -> list[str]:
    """The ONE lossless finalization command for a plan: every selected
    stream, and the chosen subtitle, copied unchanged (``-c copy``) into the
    planned container -- the native one, or Matroska when the plan chose MKV
    -- with the core metadata written as container tags. Nothing is ever
    re-encoded or filtered."""
    argv = [tools["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    inputs = [path for path, _fmt in components] + ([subtitle[0]] if subtitle is not None else [])
    for path in inputs:
        argv += ["-protocol_whitelist", "file", "-i", "file:" + path]
    for index, (_path, fmt) in enumerate(components):
        mapped = False
        if fmt.get("vcodec") not in {None, "", "none"}:
            argv += ["-map", f"{index}:v:0?"]
            mapped = True
        if fmt.get("acodec") not in {None, "", "none"}:
            argv += ["-map", f"{index}:a:0?"]
            mapped = True
        if not mapped:
            argv += ["-map", str(index)]
    if subtitle is not None:
        argv += ["-map", f"{len(components)}:s:0"]
    argv += ["-c", "copy"]
    if container in MP4_FAMILY and any(str(fmt.get("acodec") or "").startswith(("mp4a", "aac"))
                                       for _path, fmt in components):
        # ADTS-framed AAC (an HLS segment stream) re-framed for MP4: a
        # bitstream rewrite, never a decode.
        argv += ["-bsf:a", "aac_adtstoasc"]
    for key, value in metadata.items():
        argv += ["-metadata", f"{key}={value}"]
    if subtitle is not None:
        # The one preferred-language subtitle is the track players show.
        argv += ["-metadata:s:s:0", f"language={subtitle[1]}", "-disposition:s:0", "default"]
    argv += ["-f", MUXERS[container], "file:" + output]
    return argv


def acquire(spec: dict, phase: _Phase) -> int:
    from yt_dlp import YoutubeDL
    plan = spec["plan"]
    workspace, target, tools = spec["workspace"], spec["target"], spec.get("tools") or {}
    container = str(plan["container"])
    log = _Log()
    progress = _Progress()
    params = _params(spec, log, format="+".join(plan["formats"]), progress_hooks=[progress])
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    with YoutubeDL(params) as ydl:
        phase.enter("extract")
        try:
            info = ydl.extract_info(spec["url"], download=False)
        except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
            raise classify(exc, acquire=True, phase=phase.name) from None
        if not isinstance(info, dict) or info.get("_type") in {"playlist", "multi_video"} or (
                str(info.get("extractor_key") or ""), str(info.get("id") or "")) != (plan["extractor"], plan["id"]):
            raise Failure("identity_changed", "the source now identifies a different medium")
        if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
            raise Failure("live_unsupported", "live media")
        requested = info.get("requested_formats") or [info]
        if [str(item.get("format_id") or "") for item in requested] != list(plan["formats"]):
            raise Failure("format_unavailable", "the planned formats are no longer offered")
        if any(str(item.get("protocol") or "") not in NATIVE_PROTOCOLS for item in requested):
            raise Failure("transport_unsupported", "a component needs a transport outside the guard")
        # The subtitle, small and planned, is fetched first: the acquisition's
        # scope then has a known total from the start instead of only at its end.
        subtitle = None
        if plan.get("subtitle"):
            phase.enter("subtitle")
            chosen = plan["subtitle"]
            table = info.get("subtitles" if chosen["kind"] == "authored" else "automatic_captions") or {}
            track = next((item for item in table.get(chosen["language"]) or ()
                          if isinstance(item, dict) and item.get("ext") == chosen["ext"] and item.get("url")), None)
            if track is None:
                raise Failure("format_unavailable", "the planned subtitle is no longer offered")
            try:
                with ydl.urlopen(track["url"]) as response:
                    data = response.read(MAX_SUBTITLE_BYTES + 1)
            except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
                raise classify(exc, acquire=True, phase=phase.name) from None
            if not data or len(data) > MAX_SUBTITLE_BYTES:
                raise Failure("format_unavailable", "the planned subtitle could not be read")
            path = os.path.join(workspace, f"subtitle.{chosen['ext']}")
            with open(path, "wb") as handle:
                handle.write(data)
            subtitle = (path, chosen["language"])
            # One planned part, complete: its exact size is known before any
            # stream is fetched, so the whole plan's total can be.
            _emit({"event": "progress", "component": len(requested), "downloaded": len(data), "total": len(data),
                   "units": 1, "unit_total": 1, "finished": True})
        # Every planned component's exact size, and the exact fragment count of
        # one planned as fragments, before any of it is fetched: without them
        # the attempt's total stays unknown until the last component starts.
        # Only yt-dlp's exact ``filesize`` -- never the ``filesize_approx``
        # estimate -- is a total.
        for index, fmt in enumerate(requested):
            size, fragments = _count(fmt.get("filesize")), fmt.get("fragments")
            planned = len(fragments) if isinstance(fragments, list) and fragments else None
            _emit({"event": "progress", "component": index, "downloaded": 0, "total": size or None,
                   "units": 0 if planned else None, "unit_total": planned, "finished": False})
        components = []
        for index, fmt in enumerate(requested):
            item = dict(info)
            item.pop("requested_formats", None)
            item.update(fmt)
            path = os.path.join(workspace, f"component-{index}.{fmt.get('ext') or 'bin'}")
            progress.component = index
            phase.enter("component", index, fmt.get("format_id"))
            try:
                ydl.dl(path, item)
            except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
                raise classify(exc, acquire=True, phase=phase.name) from None
            if not os.path.isfile(path):
                raise Failure("output_missing", "a component was not written")
            components.append((path, fmt))
        phase.enter("finalize")
        _emit({"event": "phase", "phase": "finalize"})
        output = os.path.join(workspace, f"output.{container}")
        if container in MUXERS or len(components) > 1 or subtitle is not None:
            if container not in MUXERS:
                raise Failure("finalization_failed", "the planned container cannot be finalized losslessly")
            _run(finalization_argv(tools, container, components, subtitle, _metadata(info), output))
        else:
            # A single native file in a container this worker does not rewrite:
            # it IS the final artifact, byte for byte.
            os.replace(components[0][0], output)
        try:
            result = os.lstat(output)
        except FileNotFoundError:
            raise Failure("output_missing", "finalization produced no file") from None
        if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
            raise Failure("output_missing", "finalization produced no file")
        phase.enter("install")
        os.replace(output, target)
        size = os.lstat(target).st_size
    _record(spec, {"state": "completed", "bytes": size, "container": container})
    shutil.rmtree(workspace, ignore_errors=True)
    _emit({"event": "completed", "bytes": size})
    return 0


def _record(spec: dict, value: dict) -> None:
    """The attempt's durable completion truth, written atomically."""
    path = spec.get("result")
    if not path:
        return
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"attempt": spec.get("attempt", ""), **value}, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    spec = json.loads(sys.stdin.buffer.read(MAX_SPEC_BYTES))
    tools = {key: value for key, value in (spec.get("tools") or {}).items() if value}
    writable = tuple(path for path in (spec.get("workspace"),) if path)
    files = tuple(path for path in (spec.get("target"), spec.get("result"),
                                     spec.get("result") and spec["result"] + ".tmp") if path)
    sys.addaudithook(Sandbox(tuple(spec["guard"]), tools, writable, files, tools.get("deno")))
    # The same switch as yt-dlp's own ``--no-plugin-dirs``: no plugin directory
    # is ever searched (the parent also sets ``YTDLP_NO_PLUGINS``).
    from yt_dlp.globals import plugin_dirs
    plugin_dirs.value = []
    phase = _Phase()
    try:
        if spec.get("mode") == "extract":
            _emit({"event": "result", "facts": extract(spec)})
            return 0
        return acquire(spec, phase)
    except Exception as exc:  # noqa: BLE001 -- every failure leaves its classified truth
        failure = classify(exc, acquire=spec.get("mode") != "extract", phase=phase.name)
        if spec.get("mode") != "extract":
            try:
                _record(spec, {"state": "failed", "outcome": failure.code, "detail": failure.detail,
                               "context": phase.context()})
            except OSError:
                pass
        _emit({"event": "failure", "outcome": failure.code, "detail": failure.detail})
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
