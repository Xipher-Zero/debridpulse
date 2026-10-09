"""Media Downloads registration: ONE canonical integration owning provider + executor.

Media Downloads is a one-to-one provider/executor pairing backed by yt-dlp, so
``integrations.media`` is its single canonical namespace and
``integrations.media.enabled`` the single enable state of both halves. Like
Usenet's, this definition lives under ``integrations/`` because it is the one
module that pairs the halves: it hands the provider the sandboxed worker's
read-only extraction, so the provider never imports its executor.

Its operator settings are Target Resolution and the Video Preferences (Video
Quality, Preferred Video Codec). The Preferred
Subtitle Language it consumes is the GLOBAL Downloads preference
(``AppSettings.preferred_subtitle_language``), read here and never copied into
this namespace. There is no credential: authenticated media is out of scope.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from integrations.definition import IntegrationDefinition, IntegrationPresentation

TargetResolution = Literal["best", "2160", "1440", "1080", "720", "480", "360"]
# A relative rank among the bitrates a site already offers at the chosen
# resolution (``providers.media.plan.quality_formats``); never an encoding target.
VideoQuality = Literal["high", "normal", "low"]
# A preferred codec family among the formats a site offers; "auto" is yt-dlp's
# own codec order. Never a conversion target.
VideoCodec = Literal["auto", "av1", "hevc", "h264"]


class MediaOptions(BaseModel):
    # "best": no target, no ceiling. A resolution: exactly it when offered,
    # else the largest below it, else the smallest above it.
    target_resolution: TargetResolution = "best"
    # Auto + High (the defaults) is the native selection exactly as before.
    video_quality: VideoQuality = "high"
    video_codec: VideoCodec = "auto"

    @field_validator("video_quality", mode="before")
    @classmethod
    def _known_quality(cls, value):
        """An absent or unrecognized stored value is High."""
        value = str(value or "").strip().casefold()
        return value if value in ("high", "normal", "low") else "high"

    @field_validator("video_codec", mode="before")
    @classmethod
    def _known_codec(cls, value):
        """An absent or unrecognized stored value is Auto."""
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

    sandbox = MediaSandbox(runtime_dir(), budget=EXECUTOR_ID)
    provider = MediaProvider(sandbox.extract, target_resolution=options.target_resolution,
                             video_quality=options.video_quality, video_codec=options.video_codec,
                             subtitle_language=environment.preferred_subtitle_language)
    executor = MediaExecutor(environment.download_root, runtime_dir(), environment.repository.authorize_execution,
                             sandbox=sandbox)
    return provider, executor


definition = IntegrationDefinition(
    # ``media`` is the durable integration and provider identity; ``yt_dlp``
    # the executor's. Neither is shown to an operator.
    "media", "provider_executor", "Media Downloads", MediaOptions, build,
    durable_identities=frozenset({"media", "yt_dlp"}),
    presentation=IntegrationPresentation(
        status_name="Media Downloads",
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
    ),
)
