from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.models import TuneRequest
from app.policies import PolicyFile, load_default_policy
from app.safety import ExcludedCommand, matching_rule, prepare_statements
from app.worker import execute_action
from app.settings import Settings


def resolved(profiles, **kwargs):
    return PolicyFile(profiles=profiles, **kwargs).request()


def test_profile_teams_and_primary_source():
    policy = resolved({"index_advisor": {"enabled": True, "teams": ["DEV/OPS"]}}).policy
    rec = {"source": "index_advisor", "sources": ["global_advisor", "index_advisor"],
           "advisor_id": "order_by_index", "action_type": "CREATE_INDEX", "team": "DEV_OPS"}
    assert matching_rule(rec, policy)
    assert matching_rule({**rec, "team": "OPS"}, policy) is None
    assert matching_rule({**rec, "source": "global_advisor"}, policy) is None
    assert matching_rule({**rec, "advisor_id": "new_unreviewed_advisor"}, policy) is None
    assert {r.action_type for r in policy.rules} == {"CREATE_INDEX"}


def test_exclusions_and_unimplemented_advisors():
    request = resolved({"global_advisor": {"enabled": True, "exclude_actions": ["DROP_INDEX"],
                                           "exclude_advisors": ["Tables never vacuumed"]},
                        "parameter_advisor": {"enabled": True}})
    assert request.policy.rules
    assert all(r.source == "global_advisor" for r in request.policy.rules)
    assert not {"ALTER_TABLE", "CONFIG_CHANGE", "REVIEW_ONLY", "OTHER", "DROP_INDEX"}.intersection(r.action_type for r in request.policy.rules)
    assert all(r.advisor_id != "Tables never vacuumed" for r in request.policy.rules)


@pytest.mark.parametrize("profiles", [
    {"unknown": {"enabled": True}},
    {"index_advisor": {"enabled": True, "exclude_actions": ["DROP_INDEX"]}},
    {"global_advisor": {"exclude_actions": ["VACUM"]}},
    {"autovacuum": {"teams": ["DEVS"]}},
    {"index_advisor": {"enabled": "false"}},
    {"index_advisor": {"enable": True}},
    {"autovacuum": {"exclude_advisors": ["stale_vacum"]}},
])
def test_invalid_profiles_fail_validation(profiles):
    with pytest.raises(ValidationError):
        resolved(profiles)


def test_yaml_file_validation(tmp_path):
    path = tmp_path / "policy.yaml"
    for content in ("profiles: [", "profiles: {}\nprofiles: {}", "[]", "mode: execute\nunknown: true"):
        path.write_text(content)
        with pytest.raises(ValueError):
            load_default_policy(str(path))
    old = tmp_path / "policy.json"
    old.write_text('{}')
    with pytest.raises(ValueError, match="yaml"):
        load_default_policy(str(old))


def test_profile_expansion_is_frozen_in_queue_and_overrides_replace():
    request = resolved({"global_advisor": {"enabled": True}},
                       target_policies={"prod": {"profiles": {"autovacuum": {"enabled": True, "teams": ["OPS"]}}}})
    payload = request.model_dump(mode="json")
    assert "profiles" not in payload
    assert payload["policy"]["rules"]
    assert {r.source for r in request.target_policies["prod"].rules} == {"autovacuum"}
    assert TuneRequest.model_validate({k:v for k,v in payload.items() if k != "target_policies"}).policy == request.policy


def maintenance_rec(sql):
    return {"source": "autovacuum", "advisor_id": "stale_vacuum", "action_type": "VACUUM",
            "schema_name": "public", "table_name": "orders", "recommendation_sql": sql}


@pytest.mark.parametrize("sql,excluded", [
    ("ALTER TABLE public.orders SET (autovacuum_enabled=true); VACUUM public.orders", "ALTER_TABLE"),
    ("VACUUM (ANALYZE) public.orders", "ANALYZE"),
    ("VACUUM (ANALYZE) public.orders", "VACUUM"),
])
def test_entire_maintenance_bundle_is_blocked(sql, excluded):
    with pytest.raises(ExcludedCommand, match=excluded):
        prepare_statements(maintenance_rec(sql), "VACUUM", [excluded])


def test_reindex_and_reload_restrictions():
    rec = {"source": "global_advisor", "advisor_id": "Invalid or unusable indexes",
           "schema_name": "public", "table_name": "orders", "object_name": "idx",
           "recommendation_sql": "REINDEX INDEX public.idx"}
    assert prepare_statements(rec, "DROP_INDEX", ["DROP_INDEX"])
    with pytest.raises(ExcludedCommand, match="REINDEX"):
        prepare_statements(rec, "DROP_INDEX", ["REINDEX"])
    rec = {"source": "autovacuum", "advisor_id": "autovacuum_naptime",
           "recommendation_sql": "ALTER SYSTEM SET autovacuum_naptime='10s'"}
    for exclusion in ("ALTER_SYSTEM", "RELOAD_CONFIGURATION"):
        with pytest.raises(ExcludedCommand, match=exclusion):
            prepare_statements(rec, "CONFIG_CHANGE", [exclusion])


def test_sql_strings_are_not_commands():
    rec = {"schema_name": "public", "table_name": "orders",
           "recommendation_sql": "CREATE INDEX idx ON public.orders ((lower('ALTER TABLE')))"}
    assert prepare_statements(rec, "CREATE_INDEX", ["ALTER_TABLE"])


def test_excluded_bundle_never_connects_or_creates_steps(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    connector = MagicMock()
    monkeypatch.setattr("app.worker.psycopg.connect", connector)
    request = resolved({"autovacuum": {"enabled": True}}, mode="execute",
                       restrictions={"exclude_commands": ["ALTER_TABLE"]})
    rec = {**maintenance_rec("ALTER TABLE public.orders SET (autovacuum_enabled=true); VACUUM public.orders"),
           "id": 1, "run_id": "snapshot", "action_fingerprint": "fingerprint"}
    rule = matching_rule(rec, request.policy)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"id": 1}
    result = execute_action(Settings(database_url="postgresql://unused", api_key="test"), conn,
                            {"id": 1, "target_id": "prod"}, request, rec, rule)
    assert result == "skipped"
    connector.assert_not_called()
    assert not any("INSERT INTO autotune.action_steps" in call.args[0] for call in conn.execute.call_args_list)
    assert "SQL commands excluded by policy: ALTER_TABLE" in caplog.text


def test_unknown_sql_rejected_even_with_restrictions():
    with pytest.raises(ValueError):
        prepare_statements(maintenance_rec("VACUUM public.orders; DROP TABLE public.orders"), "VACUUM", ["ALTER_TABLE"])


def test_disabled_analyze_option_is_not_excluded():
    assert prepare_statements(maintenance_rec("VACUUM (ANALYZE false) public.orders"), "VACUUM", ["ANALYZE"])


def test_no_enabled_profiles_produces_no_matches(monkeypatch):
    from app.worker import process
    conn = MagicMock()
    collector = MagicMock()
    connector = MagicMock(return_value=collector)
    monkeypatch.setattr("app.worker.connect", connector)
    monkeypatch.setattr("app.worker.select_snapshot", lambda *args: (
        {"run_id": "snapshot", "collected_at": "2026-10-25"}, [maintenance_rec("VACUUM public.orders")]))
    request = resolved({"autovacuum": {"enabled": False}})
    run = {"id": 1, "target_id": "prod", "request": request.model_dump(mode="json", exclude={"target_policies"})}
    assert process(None, conn, run) == "no_matches"
    connector.assert_not_called()


def test_invalid_command_and_limits():
    for payload in ({"restrictions": {"exclude_commands": ["DROP_TABLE"]}},
                    {"limits": {"max_actions": 0}}, {"version": 2}):
        with pytest.raises(ValidationError):
            PolicyFile.model_validate(payload)
