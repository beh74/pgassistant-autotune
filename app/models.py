from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Selection(StrictModel):
    # Fixed at submission, never recomputed when a queued job is executed.
    lookback_seconds: int = Field(default=0, ge=0, le=31536000)
    as_of: datetime | None = None
    max_snapshot_age_seconds: int = Field(default=86400, ge=1, le=31536000)

    @model_validator(mode="after")
    def check_time(self):
        if self.as_of is not None:
            if self.as_of.tzinfo is None:
                raise ValueError("as_of must include a timezone")
            if self.lookback_seconds:
                raise ValueError("Use as_of or lookback_seconds, not both")
            if self.as_of > datetime.now(timezone.utc):
                raise ValueError("as_of cannot be in the future")
        return self

    def cutoff(self, now: datetime) -> datetime:
        return self.as_of or now - timedelta(seconds=self.lookback_seconds)


SqlCommand = Literal["CREATE_INDEX", "DROP_INDEX", "REINDEX", "ANALYZE", "VACUUM",
                     "ALTER_TABLE", "ALTER_SYSTEM", "RELOAD_CONFIGURATION"]


class Rule(StrictModel):
    advisor_id: str = Field(min_length=1)
    action_type: Literal["CREATE_INDEX", "DROP_INDEX", "VACUUM", "ANALYZE", "OTHER", "CONFIG_CHANGE", "AUTOVACUUM_TABLE_SETTINGS"]
    source: Literal["global_advisor", "index_advisor", "autovacuum", "parameter_advisor"] | None = None
    teams: list[Literal["DEV", "OPS", "DEV_OPS"]] = Field(default_factory=list)
    schemas: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    min_confidence: int = Field(default=0, ge=0, le=100)


class Policy(StrictModel):
    team: Literal["DEV", "OPS", "DEV_OPS"] | None = None
    rules: list[Rule] = Field(default_factory=list)
    exclude_commands: list[SqlCommand] = Field(default_factory=list)
    max_actions: int = Field(default=10, ge=1, le=1000)
    statement_timeout_seconds: int = Field(default=300, ge=1, le=86400)
    lock_timeout_seconds: int = Field(default=5, ge=1, le=60)


class TuneRequest(StrictModel):
    selection: Selection = Field(default_factory=Selection)
    policy: Policy
    mode: Literal["dry_run", "execute"] = "dry_run"


class TargetRequest(TuneRequest):
    target_id: str = Field(min_length=1)


class TargetReference(StrictModel):
    target_id: str = Field(min_length=1)


class AllRequest(TuneRequest):
    # Optional per-target policy overrides; never silently accept unknown IDs.
    target_policies: dict[str, Policy] = Field(default_factory=dict)
