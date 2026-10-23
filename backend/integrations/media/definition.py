"""Media Downloads registration: ONE canonical integration owning provider + executor.

Media Downloads is a one-to-one provider/executor pairing backed by yt-dlp, so
``integrations.media`` is its single canonical namespace and
``integrations.media.enabled`` the single enable state of both halves. Like
Usenet's, this definition lives under ``integrations/`` because it is the one
module that pairs the halves: it hands the provider the sandboxed worker's
read-only extraction, so the provider never imports its executor.

Its operator settings are Target Resolution, the Video Preferences (Video
Quality, Preferred Video Codec), Concurrent Fragment Downloads and YouTube
Player Clients (``youtube_player_clients``: empty is Automatic, yt-dlp's own
default; otherwise an ordered choice among the clients the PACKAGED yt-dlp
offers, read from it rather than listed here). The Preferred
Subtitle Language it consumes is the GLOBAL Downloads preference
(``AppSettings.preferred_subtitle_language``), read here and never copied into
this namespace. There is no credential: authenticated media is out of scope.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from core.settings_constraints import is_write

from integrations.definition import IntegrationDefinition, IntegrationPresentation
from integrations.media.outcomes import OPERATOR_REASONS

TargetResolution = Literal["best", "2160", "1440", "1080", "720", "480", "360"]
# A relative rank among the bitrates a site already offers at the chosen
# resolution (``providers.media.plan.quality_formats``); never an encoding target.
VideoQuality = Literal["high", "normal", "low"]
# A preferred codec family among the formats a site offers; "auto" is yt-dlp's
# own codec order. Never a conversion target.
VideoCodec = Literal["auto", "av1", "hevc", "h264"]
# The shape of one yt-dlp YouTube player-client name. A stored list outside it
# was never accepted by any version and is read as Automatic.
_PLAYER_CLIENT = re.compile(r"[a-z][a-z0-9_]{0,31}")
_MAX_STORED_PLAYER_CLIENTS = 32


@lru_cache(maxsize=1)
def supported_player_clients() -> tuple[str, ...] | None:
    """The YouTube player clients the packaged yt-dlp offers an unauthenticated
    extraction, in yt-dlp's own priority order -- read from that yt-dlp (the
    one the sandboxed worker imports), never listed here. A client yt-dlp marks
    as requiring a signed-in account is not offered: DebridPulse never signs
    in. ``None`` when the installed yt-dlp does not say: then nothing explicit
    can be validated and only Automatic is used."""
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        clients = sorted(((name, entry) for name, entry in INNERTUBE_CLIENTS.items()
                          if _PLAYER_CLIENT.fullmatch(name) and not entry.get("REQUIRE_AUTH")),
                         key=lambda item: -int(item[1]["priority"]))
    except Exception:  # noqa: BLE001 -- any change of that table's shape is "unknown"
        return None
    return tuple(name for name, _entry in clients) or None


def _player_clients_refused(supported: tuple[str, ...] | None) -> PydanticCustomError:
    if supported is None:
        return PydanticCustomError("setting_player_clients_unknown",
                                   "Only Automatic can be used: the installed yt-dlp's player clients are unknown")
    return PydanticCustomError("setting_player_clients", "Input should name each client at most once, from {supported}",
                               {"supported": ", ".join(supported)})


class MediaOptions(BaseModel):
    # "best": no target, no ceiling. A resolution: exactly it when offered,
    # else the largest below it, else the smallest above it.
    target_resolution: TargetResolution = "best"
    # Auto + High (the defaults) is the native selection exactly as before.
    video_quality: VideoQuality = "high"
    video_codec: VideoCodec = "auto"
    # yt-dlp's fragments in flight at once within ONE segmented (HLS/DASH)
    # download; a plain HTTP file is one request whatever it is. Every one of
    # them is its own guarded connection on the attempt's route, drawing on the
    # executor's one download budget. Never DebridPulse's execution concurrency.
    concurrent_fragment_downloads: int = Field(default=1, ge=1, le=8)
    # YouTube extraction's ordered player-client preference, handed to yt-dlp
    # as its ``youtube`` ``player_client`` extractor argument; empty is
    # Automatic (no argument at all). Not a DebridPulse route or retry: yt-dlp
    # asks the clients itself, once per extraction.
    youtube_player_clients: list[str] = Field(default_factory=list)

    @field_validator("youtube_player_clients", mode="before")
    @classmethod
    def _player_clients(cls, value, info: ValidationInfo):
        """A WRITTEN selection names only clients the packaged yt-dlp offers,
        each once. A STORED one is kept exactly as saved -- even a client a
        later yt-dlp no longer offers (``effective_player_clients`` then uses
        Automatic) -- unless it never had a client's shape, which reads as
        Automatic."""
        if is_write(info):
            supported = supported_player_clients()
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise _player_clients_refused(supported)
            if value and (supported is None or len(set(value)) != len(value)
                          or any(item not in supported for item in value)):
                raise _player_clients_refused(supported)
            return value
        if (isinstance(value, list) and len(value) <= _MAX_STORED_PLAYER_CLIENTS
                and all(isinstance(item, str) and _PLAYER_CLIENT.fullmatch(item) for item in value)
                and len(set(value)) == len(value)):
            return list(value)
        return []

    def unsupported_player_clients(self) -> list[str]:
        """The saved clients the packaged yt-dlp does not offer (all of them
        when it does not say which it offers)."""
        supported = supported_player_clients() or ()
        return [client for client in self.youtube_player_clients if client not in supported]

    def effective_player_clients(self) -> tuple[str, ...]:
        """What extraction actually asks for: the saved selection, or ``()``
        (Automatic) while any saved client is not offered by the packaged
        yt-dlp -- decided before any extraction, never by trying clients."""
        if self.unsupported_player_clients():
            return ()
        return tuple(self.youtube_player_clients)

    def public(self) -> dict:
        """The saved options, plus what Settings needs to present the player
        clients truthfully: what this yt-dlp offers (``None``: unknown), which
        saved ones it does not, and what extraction uses. Derived on every
        read; never stored."""
        supported = supported_player_clients()
        return {**self.model_dump(), "youtube_player_client_support": {
            "supported": list(supported) if supported is not None else None,
            "unsupported": self.unsupported_player_clients(),
            "effective": list(self.effective_player_clients()),
        }}

    @field_validator("video_quality", mode="before")
    @classmethod
    def _known_quality(cls, value, info: ValidationInfo):
        """An absent or unrecognized STORED value is High. A written one is
        exactly one of the choices -- never read as High."""
        if is_write(info):
            return value
        value = str(value or "").strip().casefold()
        return value if value in ("high", "normal", "low") else "high"

    @field_validator("video_codec", mode="before")
    @classmethod
    def _known_codec(cls, value, info: ValidationInfo):
        """An absent or unrecognized STORED value is Auto. A written one is
        exactly one of the choices -- never read as Auto."""
        if is_write(info):
            return value
        value = str(value or "").strip().casefold()
        return value if value in ("auto", "av1", "hevc", "h264") else "auto"


def runtime_dir() -> str:
    """Executor-private runtime state (process markers, attempt records,
    extraction scratch) lives beside the database: never download material."""
    from db.database import DB_PATH
    return str(Path(DB_PATH).parent / "media")


def build(options: MediaOptions, environment):
    from executors.media.executor import EXECUTOR_ID, MediaExecutor
    from executors.media.sandbox import MediaSandbox
    from providers.media.provider import MediaProvider

    # One sandbox runs both the provider's extraction and the executor's, so
    # both ask yt-dlp for the same clients.
    sandbox = MediaSandbox(runtime_dir(), budget=EXECUTOR_ID,
                           youtube_player_clients=options.effective_player_clients())
    provider = MediaProvider(sandbox.extract, target_resolution=options.target_resolution,
                             video_quality=options.video_quality, video_codec=options.video_codec,
                             subtitle_language=environment.preferred_subtitle_language)
    executor = MediaExecutor(environment.download_root, runtime_dir(), environment.repository.authorize_execution,
                             sandbox=sandbox, concurrent_fragments=options.concurrent_fragment_downloads)
    return provider, executor


definition = IntegrationDefinition(
    # ``media`` is the durable integration and provider identity; ``yt_dlp``
    # the executor's. Neither is shown to an operator.
    "media", "provider_executor", "Media Downloads", MediaOptions, build,
    durable_identities=frozenset({"media", "yt_dlp"}),
    presentation=IntegrationPresentation(
        status_name="Media Downloads",
        acquisition_method="Media Downloads",
        # A real readiness answer: the packaged tools are checked, never assumed.
        status_endpoint="/integration-status/media",
        # The next member of the Network Sources group, inside the reserved
        # STANDARD band documented on ``general_http``.
        display_order=916,
        status_group="direct_sources",
        status_group_label="Network Sources",
        status_tier="general_family",
        status_tier_label="Standard Services",
        # One transfer is "a Media Download", in the Media Downloads accent.
        transfer_label="Media Download",
        transfer_theme="hot-rose",
        error_reasons=OPERATOR_REASONS,
    ),
)
