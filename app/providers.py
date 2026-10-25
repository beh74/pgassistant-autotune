"""Extension boundary for configuration APIs (e.g. a future Autobase adapter).

No external provider is registered. Autovacuum changes use PostgreSQL directly.
The future provider receives the retained recommendation and returns its external
operation identifier for audit/reconciliation, including asynchronous operations.
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ConfigurationChange:
    target_id: str
    recommendation_id: int
    advisor_id: str
    recommendation: dict
    idempotency_key: str


@dataclass(frozen=True)
class ConfigurationResult:
    operation_id: str
    status: str


class ConfigurationProvider(Protocol):
    def submit(self, change: ConfigurationChange) -> ConfigurationResult: ...
    def inspect(self, operation_id: str) -> ConfigurationResult: ...


def execution_backend(action_type: str, source: str | None = None) -> str:
    return "configuration_api" if action_type == "CONFIG_CHANGE" and source != "autovacuum" else "postgresql"
