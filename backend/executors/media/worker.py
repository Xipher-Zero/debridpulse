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
finalization (``-c copy`` remux, or ``mkvmerge`` when the plan chose MKV) is
this worker's own, into the container the plan fixed before materialization.
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
from xml.sax.saxutils import escape

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


class Sandbox:
    """The audit hook. Decisions are recorded so a refused attempt is
    reported as the policy refusal it was, never as a generic failure."""

    _FILE_EVENTS = frozenset({"os.remove", "os.rmdir", "os.mkdir", "shutil.rmtree", "os.truncate", "os.chmod",
                              "os.chown", "os.utime"})

    def __init__(self, guard: tuple[str, int], executables: dict[str, str], writable: tuple[str, ...],
                 files: tuple[str, ...], deno: str | None):
        self.guard = (str(guard[0]), int(guard[1]))
        self.executables = {os.path.realpath(path) for path in executables.values() if path}
        self.deno = os.path.realpath(deno) if deno else None
        self.writable = tuple(os.path.realpath(path) for path in writable)
        self.files = {os.path.realpath(path) for path in files}
        self.refused: list[str] = []

    def _refuse(self, event: str):
        self.refused.append(event)
        raise PermissionError(f"DebridPulse sandbox refused {event}")

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
        if any(_NETWORK_ARGUMENT.search(item) and not _LOCAL_FILE_ARGUMENT.match(item) for item in arguments):
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


def classify(exc, sandbox: Sandbox | None, *, acquire: bool) -> Failure:
    """One yt-dlp failure, as the shared outcome vocabulary."""
    if isinstance(exc, Failure):
        return exc
    from yt_dlp.networking.exceptions import HTTPError, ProxyError, TransportError
    from yt_dlp.utils import GeoRestrictedError, UnsupportedError
    message = str(exc)
    if sandbox is not None and sandbox.refused:
        refused = sandbox.refused[0]
        code = ("egress_refused" if refused.startswith("socket.")
                else "transport_unsupported" if refused == "subprocess" else "path_refused")
        return Failure(code, message)
    for item in _chain(exc):
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


def _facts(info: dict) -> dict:
    requested = info.get("requested_formats") or [info]
    return {
        "kind": "media",
        "extractor": str(info.get("extractor_key") or ""),
        "id": str(info.get("id") or ""),
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
    }


def bounded_entries(info: dict, bound: int) -> list:
    """Every entry of a collection, or a refusal: a collection larger than
    the bound is never truncated into a smaller one (the extraction asked for
    one entry more than the bound, so reaching it proves the excess)."""
    entries = list(info.get("entries") or [])
    if len(entries) > bound:
        raise Failure("collection_too_large", f"more than {bound} entries")
    return entries


def extract(spec: dict, sandbox: Sandbox) -> dict:
    """Read-only facts for the provider's plan: one medium, or a complete
    bounded collection whose every member was itself planned (or failed)."""
    from yt_dlp import YoutubeDL
    selection = spec.get("selection") or {}
    bound = int(spec.get("collection_bound") or 0)
    log = _Log()
    params = _params(spec, log, format=selection.get("format"), format_sort=list(selection.get("format_sort") or []),
                     extract_flat="in_playlist", playlistend=bound + 1, lazy_playlist=False)
    with YoutubeDL(params) as ydl:
        try:
            info = ydl.extract_info(spec["url"], download=False)
        except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
            raise classify(exc, sandbox, acquire=False) from None
        if not isinstance(info, dict):
            raise Failure("extractor_failed", "no information")
        if info.get("_type") not in {"playlist", "multi_video"}:
            return _facts(info)
        entries = bounded_entries(info, bound)
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
                failure = classify(exc, sandbox, acquire=False)
                if failure.code in {"egress_refused", "transport_unsupported", "network", "rate_limited"}:
                    raise failure from None
                member["outcome"] = failure.code
            members.append(member)
        return {"kind": "collection", "extractor": str(info.get("extractor_key") or ""),
                "id": str(info.get("id") or ""), "title": str(info.get("title") or "")[:512], "members": members}


class _Progress:
    """yt-dlp progress, per component, emitted no more often than the interval."""

    def __init__(self):
        self.component = 0
        self.last = 0.0

    def __call__(self, status: dict) -> None:
        now = time.monotonic()
        finished = status.get("status") == "finished"
        if not finished and now - self.last < PROGRESS_INTERVAL:
            return
        self.last = now
        total = status.get("total_bytes")
        _emit({"event": "progress", "component": self.component,
               "downloaded": int(status.get("downloaded_bytes") or 0),
               "total": int(total) if isinstance(total, (int, float)) and total > 0 else None,
               "finished": finished})


def _run(argv: list[str], failure: str) -> None:
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise Failure("runtime_unavailable", str(exc)) from None
    # mkvmerge exits 1 for warnings with a complete output; 2 is an error.
    acceptable = {0, 1} if failure == "mkvmerge" else {0}
    if completed.returncode not in acceptable:
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
                      metadata: dict, output: str, workspace: str) -> list[str]:
    """The ONE lossless finalization command for a plan: every stream copied
    unchanged into the planned container (``-c copy``), or -- only when the
    plan chose MKV -- merged by ``mkvmerge``, which never re-encodes."""
    if container == "mkv":
        tags = os.path.join(workspace, "tags.xml")
        simple = "".join(f"<Simple><Name>{escape(key.upper())}</Name><String>{escape(value)}</String></Simple>"
                         for key, value in metadata.items() if key != "date")
        if metadata.get("date"):
            simple += f"<Simple><Name>DATE_RELEASED</Name><String>{escape(metadata['date'])}</String></Simple>"
        with open(tags, "w", encoding="utf-8") as handle:
            handle.write('<?xml version="1.0" encoding="UTF-8"?>\n<Tags><Tag><Targets>'
                         f"<TargetTypeValue>50</TargetTypeValue></Targets>{simple}</Tag></Tags>\n")
        argv = [tools["mkvmerge"], "--quiet", "--output", output, "--global-tags", tags]
        if metadata.get("title"):
            argv += ["--title", metadata["title"]]
        argv += [path for path, _fmt in components]
        if subtitle is not None:
            path, language = subtitle
            argv += ["--language", f"0:{language}", path]
        return argv
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
        argv += ["-metadata:s:s:0", f"language={subtitle[1]}"]
    argv += ["-f", MUXERS[container], "file:" + output]
    return argv


def acquire(spec: dict, sandbox: Sandbox) -> int:
    from yt_dlp import YoutubeDL
    plan = spec["plan"]
    workspace, target, tools = spec["workspace"], spec["target"], spec.get("tools") or {}
    container = str(plan["container"])
    log = _Log()
    progress = _Progress()
    params = _params(spec, log, format="+".join(plan["formats"]), progress_hooks=[progress])
    os.makedirs(workspace, mode=0o700, exist_ok=True)
    with YoutubeDL(params) as ydl:
        try:
            info = ydl.extract_info(spec["url"], download=False)
        except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
            raise classify(exc, sandbox, acquire=True) from None
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
        components = []
        for index, fmt in enumerate(requested):
            item = dict(info)
            item.pop("requested_formats", None)
            item.update(fmt)
            path = os.path.join(workspace, f"component-{index}.{fmt.get('ext') or 'bin'}")
            progress.component = index
            try:
                ydl.dl(path, item)
            except Exception as exc:  # noqa: BLE001 -- classified, never swallowed
                raise classify(exc, sandbox, acquire=True) from None
            if not os.path.isfile(path):
                raise Failure("output_missing", "a component was not written")
            components.append((path, fmt))
        subtitle = None
        if plan.get("subtitle"):
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
                raise classify(exc, sandbox, acquire=True) from None
            if not data or len(data) > MAX_SUBTITLE_BYTES:
                raise Failure("format_unavailable", "the planned subtitle could not be read")
            path = os.path.join(workspace, f"subtitle.{chosen['ext']}")
            with open(path, "wb") as handle:
                handle.write(data)
            subtitle = (path, chosen["language"])
        _emit({"event": "phase", "phase": "finalize"})
        output = os.path.join(workspace, f"output.{container}")
        if container in MUXERS or len(components) > 1 or subtitle is not None:
            if container not in MUXERS:
                raise Failure("finalization_failed", "the planned container cannot be finalized losslessly")
            argv = finalization_argv(tools, container, components, subtitle, _metadata(info), output, workspace)
            _run(argv, "mkvmerge" if container == "mkv" else "ffmpeg")
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
    sandbox = Sandbox(tuple(spec["guard"]), tools, writable, files, tools.get("deno"))
    sys.addaudithook(sandbox)
    # The same switch as yt-dlp's own ``--no-plugin-dirs``: no plugin directory
    # is ever searched (the parent also sets ``YTDLP_NO_PLUGINS``).
    from yt_dlp.globals import plugin_dirs
    plugin_dirs.value = []
    try:
        if spec.get("mode") == "extract":
            _emit({"event": "result", "facts": extract(spec, sandbox)})
            return 0
        return acquire(spec, sandbox)
    except Exception as exc:  # noqa: BLE001 -- every failure leaves its classified truth
        failure = classify(exc, sandbox, acquire=spec.get("mode") != "extract")
        if spec.get("mode") != "extract":
            try:
                _record(spec, {"state": "failed", "outcome": failure.code, "detail": failure.detail})
            except OSError:
                pass
        _emit({"event": "failure", "outcome": failure.code, "detail": failure.detail})
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
