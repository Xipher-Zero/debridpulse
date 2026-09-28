"""rsync executor registration and executor-owned settings schema.

Exactly five operator tunables, each an rsync-native transport behaviour this
executor implements truthfully; nothing here is universal transfer policy, and
there is deliberately no free-form native argument of any kind.
"""
from pathlib import Path

from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition


class RsyncOptions(BaseModel):
    # Continue an interrupted file in place from the DebridPulse-authorized
    # prefix; off, nothing partial ever reaches the destination.
    partial_transfers: bool = True
    # Native transport compression. Off by default: most payloads are already
    # compressed archives or media.
    compression: bool = False
    # Give a completed file the source's modification time; never evidence of
    # anything.
    preserve_modification_time: bool = True
    # How long reaching the source and opening the file may take before the
    # transfer begins (the SSH channel's connection and login share it).
    connection_timeout_seconds: int = Field(default=30, ge=5, le=300)
    # How long a running transfer may go without any data before it stops
    # (rsync --timeout); never a replacement for DebridPulse's stalled policy.
    transfer_timeout_seconds: int = Field(default=300, ge=30, le=3600)


def runtime_dir() -> str:
    """Executor-private runtime state (process ownership markers, evidence
    scratch) lives beside the database: application state, never material."""
    from db.database import DB_PATH
    return str(Path(DB_PATH).parent / "rsync")


def build(options, environment):
    from executors.rsync.executor import RsyncConfiguration, RsyncExecutor
    configuration = RsyncConfiguration(
        environment.download_root, runtime_dir(),
        partial_transfers=options.partial_transfers, compression=options.compression,
        preserve_modification_time=options.preserve_modification_time,
        connection_timeout_seconds=options.connection_timeout_seconds,
        transfer_timeout_seconds=options.transfer_timeout_seconds,
    )
    return RsyncExecutor(configuration, environment.repository.authorize_execution)


definition = IntegrationDefinition("rsync", "executor", "rsync", RsyncOptions, build)
