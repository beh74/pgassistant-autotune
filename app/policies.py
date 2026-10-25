"""YAML profiles are expanded at submission into immutable, explicit queue rules."""
import argparse
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, StrictBool, model_validator

from app.models import AllRequest, Policy, Rule, Selection, SqlCommand, StrictModel
from app.profile_catalog import EXECUTABLE_RECOMMENDATIONS, PROFILE_ACTIONS, PROFILE_ADVISORS


class Profile(StrictModel):
    enabled: StrictBool = False
    teams: list[Literal["DEV", "OPS", "DEV/OPS", "DEV_OPS"]] = Field(default_factory=list)
    exclude_actions: list[str] = Field(default_factory=list)
    exclude_advisors: list[str] = Field(default_factory=list)
    schemas: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    min_confidence: int = Field(default=0, ge=0, le=100)


class Restrictions(StrictModel):
    exclude_commands: list[SqlCommand] = Field(default_factory=list)


class Limits(StrictModel):
    max_actions: int = Field(default=10, ge=1, le=1000)
    statement_timeout_seconds: int = Field(default=300, ge=1, le=86400)
    lock_timeout_seconds: int = Field(default=5, ge=1, le=60)


class ProfilePolicy(StrictModel):
    profiles: dict[str, Profile] = Field(default_factory=dict)
    restrictions: Restrictions = Field(default_factory=Restrictions)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def validate_profiles(self):
        for name, profile in self.profiles.items():
            if name not in PROFILE_ACTIONS:
                raise ValueError(f"Unknown profile: {name}; choose from {', '.join(PROFILE_ACTIONS)}")
            invalid = set(profile.exclude_actions) - PROFILE_ACTIONS[name]
            if invalid:
                raise ValueError(f"Invalid exclude_actions for {name}: {', '.join(sorted(invalid))}")
            invalid_advisors = set(profile.exclude_advisors) - PROFILE_ADVISORS[name]
            if invalid_advisors:
                raise ValueError(f"Unknown exclude_advisors for {name}: {', '.join(sorted(invalid_advisors))}")
        return self

    def resolve(self) -> Policy:
        rules = []
        for source, advisor, action in EXECUTABLE_RECOMMENDATIONS:
            profile = self.profiles.get(source)
            if not profile or not profile.enabled or action in profile.exclude_actions or advisor in profile.exclude_advisors:
                continue
            rules.append(Rule(source=source, advisor_id=advisor, action_type=action,
                              teams=list(dict.fromkeys("DEV_OPS" if team == "DEV/OPS" else team for team in profile.teams)),
                              schemas=profile.schemas, tables=profile.tables, min_confidence=profile.min_confidence))
        return Policy(rules=rules, exclude_commands=self.restrictions.exclude_commands, **self.limits.model_dump())


class PolicyFile(ProfilePolicy):
    version: Literal[1] = 1
    mode: Literal["dry_run", "execute"] = "dry_run"
    selection: Selection = Field(default_factory=Selection)
    # Complete replacements, rather than an implicit merge with the default profiles.
    target_policies: dict[str, ProfilePolicy] = Field(default_factory=dict)

    def request(self) -> AllRequest:
        return AllRequest(mode=self.mode, selection=self.selection, policy=self.resolve(),
                          target_policies={target: policy.resolve() for target, policy in self.target_policies.items()})


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject accidental duplicate keys instead of silently changing authorization."""


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Policy YAML requires unique string mapping keys")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def read_policy_file(path: str | None) -> PolicyFile:
    if not path:
        raise ValueError("No default policy configured")
    if Path(path).suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError("Policy file must use .yaml or .yml")
    try:
        payload = yaml.load(Path(path).read_text(), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError("Invalid policy YAML") from exc
    return PolicyFile.model_validate(payload)


def load_default_policy(path: str | None) -> AllRequest:
    """Read and validate on submission; the queued task retains its resolved copy."""
    return read_policy_file(path).request()


def main():
    parser = argparse.ArgumentParser(description="Validate a YAML policy and preview its resolved rules (no database access).")
    parser.add_argument("path", help="Path to policy.yaml")
    args = parser.parse_args()
    try:
        configured = read_policy_file(args.path)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Invalid policy: {exc}\n")
    request = configured.request()
    for name, profile in configured.profiles.items():
        count = sum(rule.source == name for rule in request.policy.rules)
        print(f"{name}: enabled={profile.enabled} executable_rules={count}")
        if profile.enabled and not count:
            print("  No executable rules: excluded actions or unsupported advisor.")
    print(yaml.safe_dump(request.model_dump(mode="json"), sort_keys=False), end="")


if __name__ == "__main__":
    main()
