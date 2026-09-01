"""aria2 registration and executor-owned settings schema."""
from typing import Literal
from pydantic import BaseModel, Field

from integrations.definition import IntegrationDefinition


class Aria2Options(BaseModel):
    mode: Literal["builtin", "external"] = "builtin"
    url: str = "http://127.0.0.1:6800/jsonrpc"
    secret: str = Field(default="", repr=False)
    builtin_port: int = Field(default=6800, ge=1, le=65535)
    download_path: str = ""
    operation_timeout_seconds: int = Field(default=15, ge=1)
    split: int = Field(default=16, ge=1)
    min_split_size: str = "10M"
    max_connection_per_server: int = Field(default=16, ge=1)
    continue_downloads: bool = True
    waiting_window: int = Field(default=100, ge=10, le=1000)
    stopped_window: int = Field(default=100, ge=10, le=1000)


def build(options, environment):
    from executors.aria2.client import Aria2Service
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from executors.aria2.runtime import effective_rpc_config
    # Runtime deployment settings remain an explicit executor concern.
    from core.config import get_settings
    url, secret = effective_rpc_config(get_settings()) if options.mode == "builtin" else (options.url, options.secret)
    client = Aria2Service(url, secret, options.operation_timeout_seconds)
    configuration = Aria2Configuration(
        environment.download_root, options.download_path if options.mode == "external" else "", options.mode == "external",
        options.split, options.min_split_size, options.max_connection_per_server, options.continue_downloads,
        waiting_window=options.waiting_window, stopped_window=options.stopped_window,
        secrets=(secret,),
    )
    return Aria2Executor(client, configuration, environment.repository.authorize_execution)


definition = IntegrationDefinition(
    "aria2", "executor", "aria2", Aria2Options, build,
    secret_fields=frozenset({"secret"}),
    legacy_fields=tuple(("aria2_" + field, field) for field in Aria2Options.model_fields),
    ownership_fields=frozenset({"mode", "url", "builtin_port", "download_path"}),
)
