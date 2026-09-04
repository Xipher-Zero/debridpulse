"""Configuration metadata shared by modular integration definitions."""
from dataclasses import dataclass, replace
from typing import Callable, Optional

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
class IntegrationPresentation:
    """Safe, provider-owned presentation facts for neutral UI surfaces.

    Operational state is never inferred from enabled/configured state. Providers
    either expose a status endpoint or explicitly declare a local static state.
    """

    status_name: Optional[str] = None
    premium: bool = False
    status_endpoint: Optional[str] = None
    static_status: Optional[str] = None
    display_order: int = 100

    def public(self) -> dict:
        return {
            "status_name": self.status_name,
            "premium": self.premium,
            "status_endpoint": self.status_endpoint,
            "static_status": self.static_status,
            "display_order": self.display_order,
        }


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
    required_options: frozenset[str] = frozenset()
    presentation: IntegrationPresentation = IntegrationPresentation()

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

    def configured(self, options: dict) -> bool:
        """Return persisted configuration presence without exposing secret data."""
        validated = self.options_model(**options).model_dump()
        for key in self.required_options:
            value = validated.get(key)
            if isinstance(value, str):
                if not value.strip():
                    return False
            elif not value:
                return False
        return True
