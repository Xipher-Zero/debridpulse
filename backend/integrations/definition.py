"""Configuration metadata shared by modular integration definitions."""
from dataclasses import dataclass, replace
from typing import Callable

from pydantic import BaseModel, Field


class IntegrationSettings(BaseModel):
    enabled: bool = True
    priority: int = 0
    options: dict = Field(default_factory=dict, repr=False)
    clear_secrets: list[str] = Field(default_factory=list, exclude=True)


@dataclass(frozen=True)
class IntegrationEnvironment:
    repository: object
    download_root: str


@dataclass(frozen=True)
class IntegrationDefinition:
    id: str
    kind: str
    name: str
    options_model: type[BaseModel]
    factory: Callable
    secret_fields: frozenset[str] = frozenset()
    legacy_fields: tuple[tuple[str, str], ...] = ()
    ownership_fields: frozenset[str] = frozenset()

    def build(self, settings: IntegrationSettings, environment: IntegrationEnvironment):
        implementation = self.factory(self.options_model(**settings.options), environment)
        implementation.descriptor = replace(implementation.descriptor,
            enabled=implementation.descriptor.enabled and settings.enabled, priority=settings.priority)
        return implementation

    def public_options(self, options: dict):
        result = self.options_model(**options).model_dump()
        for key in self.secret_fields:
            result[key + "_configured"] = bool(result.get(key))
            result[key] = ""
        return result
