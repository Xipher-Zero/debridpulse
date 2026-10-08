"""Media Downloads registration: ONE canonical integration owning provider + executor.

Media Downloads is a one-to-one provider/executor pairing backed by yt-dlp, so
``integrations.media`` is its single canonical namespace and
``integrations.media.enabled`` the single enable state of both halves. Like
Usenet's, this definition lives under ``integrations/`` because it is the one
module that pairs the halves: it hands the provider the sandboxed worker's
read-only extraction, so the provider never imports its executor.

Its one operator setting is Target Resolution. The Preferred Subtitle Language
it consumes is the GLOBAL Downloads preference (``AppSettings
.preferred_subtitle_language``), read here and never copied into this
namespace. There is no credential: authenticated media is out of scope.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from integrations.definition import IntegrationDefinition, IntegrationPresentation

TargetResolution = Literal["best", "2160", "1440", "1080", "720", "480", "360"]


class MediaOptions(BaseModel):
    # "best": no target, no ceiling. A resolution: exactly it when offered,
    # else the largest below it, else the smallest above it.
    target_resolution: TargetResolution = "best"


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
        # One transfer is "a Media Download", themed Hot Rose.
        transfer_label="Media Download",
        transfer_theme="hot-rose",
    ),
)
