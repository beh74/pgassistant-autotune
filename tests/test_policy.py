from datetime import datetime, timedelta, timezone
import yaml
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.models import AllRequest, Policy, Selection
from app.safety import matching_rule, prepare_sql
from app.settings import Settings
from app.sources import execution_uri, load_sources


def recommendation(sql="CREATE INDEX idx ON public.orders (customer_id)"):
    return {"advisor_id": "missing_useful_foreign_key_indexes", "action_type": "CREATE_INDEX", "source": "global_advisor", "schema_name": "public", "table_name": "orders", "confidence": 85, "recommendation_sql": sql}


def test_exact_rule_selection():
    policy = Policy(rules=[{"advisor_id": "missing_useful_foreign_key_indexes", "action_type": "CREATE_INDEX"}])
    assert matching_rule(recommendation(), policy)
    assert not matching_rule({**recommendation(), "advisor_id": "order_by_index"}, policy)
    assert not matching_rule({**recommendation(), "action_type": "DROP_INDEX"}, policy)


def test_index_concurrent_and_qualified():
    sql = prepare_sql(recommendation(), "CREATE_INDEX")
    assert "CONCURRENTLY IF NOT EXISTS" in sql


@pytest.mark.parametrize("sql", [
    "DROP TABLE public.orders", "CREATE INDEX idx ON public.orders (customer_id); DROP TABLE public.orders",
    "CREATE INDEX idx ON other.orders (customer_id)", "CREATE INDEX idx ON orders (customer_id)",
])
def test_reject_unsafe_or_mismatched_index(sql):
    with pytest.raises(ValueError):
        prepare_sql(recommendation(sql), "CREATE_INDEX")


def test_autovacuum_extraction():
    rec = recommendation("ALTER TABLE public.orders SET (autovacuum_vacuum_scale_factor=0.05); ANALYZE public.orders; VACUUM public.orders;")
    sql = prepare_sql(rec, "AUTOVACUUM_TABLE_SETTINGS")
    assert sql.startswith("ALTER TABLE")
    assert "VACUUM public" not in sql and "ANALYZE" not in sql


@pytest.mark.parametrize("sql", ["ALTER TABLE public.orders DROP COLUMN customer_id", "ALTER TABLE public.orders SET (fillfactor=20)", "ALTER SYSTEM SET autovacuum=off", "VACUUM public.orders"])
def test_autovacuum_reject_other_actions(sql):
    with pytest.raises(ValueError):
        prepare_sql(recommendation(sql), "AUTOVACUUM_TABLE_SETTINGS")


def test_extended_index_definition_preserved():
    sql = prepare_sql(recommendation("CREATE INDEX idx ON public.orders ((lower(name))) INCLUDE (customer_id) WHERE customer_id IS NOT NULL"), "CREATE_INDEX")
    assert "lower(name)" in sql and "INCLUDE" in sql and "WHERE" in sql


def test_autovacuum_bundle_and_source_filter():
    from app.safety import prepare_statements
    rec = {**recommendation("ALTER TABLE public.orders SET (autovacuum_enabled=true); ANALYZE public.orders; VACUUM (ANALYZE) public.orders"), "source": "autovacuum", "action_type": "VACUUM"}
    assert len(prepare_statements(rec, "VACUUM")) == 3
    policy = Policy(rules=[{"advisor_id": rec["advisor_id"], "action_type": "VACUUM", "source": "global_advisor"}])
    assert matching_rule(rec, policy) is None


@pytest.mark.parametrize("sql", ["VACUUM", "VACUUM other.orders", "ANALYZE public.orders; DROP TABLE public.orders"])
def test_maintenance_boundaries(sql):
    with pytest.raises(ValueError):
        prepare_sql({**recommendation(sql), "source": "autovacuum"}, "VACUUM")


def test_drop_and_reindex():
    rec = {**recommendation("DROP INDEX public.idx"), "object_name": "idx"}
    assert "CONCURRENTLY IF EXISTS" in prepare_sql(rec, "DROP_INDEX")
    with pytest.raises(ValueError):
        prepare_sql({**rec, "recommendation_sql": "DROP INDEX public.idx CASCADE"}, "DROP_INDEX")
    rec.update(advisor_id="Invalid or unusable indexes", recommendation_sql="REINDEX INDEX public.idx")
    assert prepare_sql(rec, "DROP_INDEX").startswith("REINDEX INDEX")


def test_configuration_never_uses_sql_executor():
    from app.providers import execution_backend
    from app.safety import UnsupportedAction
    assert execution_backend("CONFIG_CHANGE") == "configuration_api"
    with pytest.raises(UnsupportedAction):
        prepare_sql(recommendation("ALTER SYSTEM SET work_mem='1GB'"), "CONFIG_CHANGE")


def test_temporal_contract():
    now = datetime.now(timezone.utc)
    assert Selection(lookback_seconds=3600).cutoff(now) == now - timedelta(hours=1)
    with pytest.raises(ValidationError):
        Selection(as_of=datetime(2020, 1, 1))
    with pytest.raises(ValidationError):
        Selection(as_of=now, lookback_seconds=10)


def test_collector_yaml_compatibility(tmp_path, monkeypatch):
    path = tmp_path / "sources.yaml"
    path.write_text('defaults:\n  pgassistant_api_url: http://pgassistant\nsources:\n  - id: prod\n    conn_str: postgresql://reader:${UNSET_SECRET}@db/app\n    autotune_uri: ${TEST_EXEC_URI}\n')
    monkeypatch.setenv("TEST_EXEC_URI", "postgresql://operator@db/app?sslmode=require")
    sources = load_sources(str(path))
    assert execution_uri(sources["prod"]) == "postgresql://operator@db/app?sslmode=require"
    assert execution_uri({"conn_str": "postgresql://operator@db/app"}) == "postgresql://operator@db/app"
    assert execution_uri({"db_uri": "postgresql://preferred@db/app", "conn_str": "postgresql://legacy@db/app"}) == "postgresql://preferred@db/app"
    with pytest.raises(ValueError):
        execution_uri({})


def test_duplicate_targets_rejected(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text("sources:\n  - id: prod\n  - id: prod\n")
    with pytest.raises(ValueError):
        load_sources(str(path))


def test_api_auth_and_validation():
    client = TestClient(create_app(Settings(database_url="postgresql://unused", api_key="test-key")))
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/runs").status_code == 401
    assert client.post("/api/v1/autotune-all", headers={"X-API-Key": "test-key"}, json={"policy": {"unknown_setting": True}}).status_code == 422


def test_policy_defaults_are_disabled_and_safe():
    from app.policies import PolicyFile
    request = PolicyFile().request()
    assert request.mode == "dry_run"
    assert request.policy.rules == []




@pytest.mark.parametrize("run_id", ["00000000-0000-0000-0000-000000000001", "0", "-1", "9223372036854775808"])
def test_run_endpoint_requires_positive_bigint(run_id):
    client = TestClient(create_app(Settings(database_url="postgresql://unused", api_key="test-key")))
    assert client.get(f"/api/v1/runs/{run_id}", headers={"X-API-Key": "test-key"}).status_code == 422


def test_ops_policy_filters_actual_collector_team():
    from app.policies import load_default_policy
    request = load_default_policy(str(Path(__file__).resolve().parents[1] / "config/policy_ops.yaml"))
    assert request.mode == "execute"
    assert {rule.action_type for rule in request.policy.rules} == {"ANALYZE", "VACUUM", "CONFIG_CHANGE"}
    rec = {"source": "global_advisor", "advisor_id": "Tables with estimated bloat", "action_type": "VACUUM", "team": "OPS"}
    assert matching_rule(rec, request.policy)
    for team in ("DEV_OPS", "DEV", None):
        assert matching_rule({**rec, "team": team}, request.policy) is None
    config_rules = {r.advisor_id for r in request.policy.rules if r.action_type == "CONFIG_CHANGE"}
    assert config_rules == {"autovacuum_vacuum_scale_factor", "autovacuum_analyze_scale_factor"}


def test_default_policy_submission_and_override(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    policy_path = tmp_path / "policy.yaml"
    sources = tmp_path / "sources.yaml"
    sources.write_text("sources:\n  - id: prod\n    db_uri: postgresql://unused\n")
    configured = {"mode": "dry_run", "profiles": {"global_advisor": {"enabled": True, "teams": ["OPS"]}},
                  "target_policies": {"prod": {"profiles": {"index_advisor": {"enabled": True}}}}}
    policy_path.write_text(yaml.safe_dump(configured))
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"id": 1}
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr("app.main.connect", lambda settings: context)
    client = TestClient(create_app(Settings(database_url="postgresql://unused", api_key="test-key", sources_path=str(sources), policy_path=str(policy_path))))
    headers = {"X-API-Key": "test-key"}
    assert client.post("/api/v1/autotune-all", headers=headers).status_code == 202
    payload = conn.execute.call_args.args[1][1].obj
    assert payload["policy"]["rules"][0]["source"] == "index_advisor"
    assert client.post("/api/v1/autotune-targetid", headers=headers, json={"target_id": "prod"}).status_code == 202
    assert client.post("/api/v1/autotune-targetid?target_id=prod", headers=headers).status_code == 202
    assert client.post("/api/v1/autotune-targetid", headers=headers).status_code == 422
    explicit = {"policy": {"rules": [{"advisor_id": "explicit", "action_type": "CREATE_INDEX"}]}}
    policy_path.write_text("profiles: [invalid")
    assert client.post("/api/v1/autotune-all", headers=headers).status_code == 503
    assert client.post("/api/v1/autotune-all", headers=headers, json=explicit).status_code == 202
    payload = conn.execute.call_args.args[1][1].obj
    assert payload["policy"]["rules"][0]["advisor_id"] == "explicit"
    assert "team" in payload["policy"] and payload["policy"]["team"] is None
    assert client.post("/api/v1/autotune-targetid", headers=headers, json={**explicit, "target_id": "prod"}).status_code == 202
    assert client.post("/api/v1/autotune-all", headers=headers, json={"policy": {"unknown_setting": True}}).status_code == 422


def test_no_default_policy_configured():
    client = TestClient(create_app(Settings(database_url="postgresql://unused", api_key="test-key", policy_path=None)))
    assert client.post("/api/v1/autotune-all", headers={"X-API-Key": "test-key"}).status_code == 503


def config_rec(sql="ALTER SYSTEM SET autovacuum_naptime='10s'"):
    return {"source": "autovacuum", "advisor_id": "autovacuum_naptime",
            "action_type": "CONFIG_CHANGE", "recommendation_sql": sql}


def test_autovacuum_configuration_execution():
    from app.providers import execution_backend
    assert execution_backend("CONFIG_CHANGE", "autovacuum") == "postgresql"
    sql = prepare_sql(config_rec(), "CONFIG_CHANGE")
    assert sql.startswith("ALTER SYSTEM SET autovacuum_naptime")
    assert sql.endswith("SELECT pg_catalog.pg_reload_conf();")


@pytest.mark.parametrize("sql", [
    "ALTER SYSTEM SET work_mem='1GB'", "ALTER SYSTEM RESET autovacuum_naptime",
    "ALTER SYSTEM RESET ALL", "ALTER SYSTEM SET autovacuum='off'",
    "ALTER SYSTEM SET autovacuum_naptime='10s'; DROP TABLE public.orders",
    "SELECT pg_reload_conf()", "ALTER SYSTEM SET autovacuum_naptime=DEFAULT",
])
def test_autovacuum_configuration_rejects_unapproved_sql(sql):
    with pytest.raises(ValueError):
        prepare_sql(config_rec(sql), "CONFIG_CHANGE")


def test_autovacuum_configuration_restart_deferred():
    from app.safety import UnsupportedAction
    with pytest.raises(UnsupportedAction):
        prepare_sql({**config_rec(), "requires_restart": True}, "CONFIG_CHANGE")


@pytest.mark.parametrize("context, expected", [("sighup", "succeeded"), ("postmaster", "deferred"), ("connection_error", "failed")])
def test_configuration_worker_without_table_metadata(monkeypatch, caplog, context, expected):
    import logging
    import psycopg
    caplog.set_level(logging.INFO, logger="app.worker")
    from unittest.mock import MagicMock
    from app.worker import execute_action
    from app.models import TuneRequest
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [{"id": 1}, None]
    target = MagicMock()
    target.execute.return_value.fetchone.side_effect = [
        (False,), ("postgres",), ("autovacuum_naptime", "60", "s", context, False), (True,),
    ]
    connection = MagicMock()
    connection.__enter__.return_value = target
    if context == "connection_error":
        connection.__enter__.side_effect = psycopg.OperationalError("postgresql://operator:secret-password@host/db")
    monkeypatch.setattr("app.worker.psycopg.connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr("app.worker.load_sources", lambda path: {"test": {"db_uri": "postgresql://unused"}})
    request = TuneRequest.model_validate({"mode": "execute", "policy": {"rules": [
        {"advisor_id": "autovacuum_naptime", "action_type": "CONFIG_CHANGE"}]}})
    rec = {**config_rec(), "id": 1, "run_id": "snapshot", "action_fingerprint": "fingerprint",
           "database_name": "postgres"}
    status = execute_action(Settings(database_url="postgresql://unused", api_key="test"), conn,
                            {"id": 1, "target_id": "test"}, request, rec, request.policy.rules[0])
    assert status == expected
    queries = [call.args[0] for call in target.execute.call_args_list]
    assert not any("pg_class" in query for query in queries)
    assert any(query.startswith("ALTER SYSTEM") for query in queries) == (expected == "succeeded")
    assert ("SELECT pg_catalog.pg_reload_conf();" in queries) == (expected == "succeeded")

    assert "run_id=1 target_id='test' action_id=1" in caplog.text
    assert f"action finished status={expected}" in caplog.text
    assert "secret-password" not in caplog.text
    if expected == "succeeded":
        assert "step started step=1/2 command=AlterSystemStmt" in caplog.text
        assert "step finished step=2/2 status=succeeded duration_ms=" in caplog.text
    elif expected == "failed":
        assert "error=OperationalError; SQLSTATE=n/a" in caplog.text


@pytest.mark.parametrize("status, errors", [
    ("partial", [{"source": "parameter_advisor", "error": "secret-diagnostic"}]),
    ("ok", [{"source": "parameter_advisor", "error": "secret-diagnostic"}]),
    ("partial", []),
])
def test_incomplete_snapshot_diagnostic(caplog, status, errors):
    from unittest.mock import MagicMock
    from app.models import TuneRequest
    from app.worker import select_snapshot, IncompleteSnapshot
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {
        "run_id": "snapshot-30", "status": status, "advisor_errors": errors,
    }
    run = {"id": 30, "target_id": "collector-demo", "cutoff": datetime.now(timezone.utc)}
    request = TuneRequest.model_validate({"policy": {"rules": [
        {"advisor_id": "autovacuum_naptime", "action_type": "CONFIG_CHANGE"}]}})
    with pytest.raises(IncompleteSnapshot) as failure:
        select_snapshot(conn, run, request)
    assert "snapshot_id=snapshot-30" in str(failure.value)
    assert f"advisor_error_count={len(errors)}" in str(failure.value)
    assert "run_id=30" in caplog.text
    assert f"status={status!r}" in caplog.text
    if errors:
        assert "parameter_advisor" in caplog.text
    assert "secret-diagnostic" not in caplog.text + str(failure.value)
    assert conn.execute.call_count == 1


@pytest.mark.parametrize("sql, schema, object_name", [
    ("DROP INDEX public.idx", "public", "idx"),
    ("DROP INDEX public.idx", "public", "public.idx"),
    ('DROP INDEX "Sales"."Order.Index"', "Sales", '"Sales"."Order.Index"'),
    ('DROP INDEX public."odd""index"', "public", 'public."odd""index"'),
    ("REINDEX INDEX public.idx", "public", "public.idx"),
])
def test_index_metadata_accepts_qualified_names(sql, schema, object_name):
    rec = {**recommendation(sql), "schema_name": schema, "object_name": object_name,
           "advisor_id": "Invalid or unusable indexes"}
    assert prepare_sql(rec, "DROP_INDEX")


@pytest.mark.parametrize("object_name", [
    "other.idx", "public.different", 'public."IDX"',
    "public.idx; DROP TABLE public.orders", "public.idx, public.other", None,
])
def test_index_metadata_rejects_different_identity(object_name):
    rec = {**recommendation("DROP INDEX public.idx"), "object_name": object_name}
    with pytest.raises(ValueError):
        prepare_sql(rec, "DROP_INDEX")


@pytest.mark.parametrize("sql, protected", [
    ("DROP INDEX public.idx", False),
    ("REINDEX INDEX public.idx", False),
    ("DROP INDEX public.idx", True),
])
def test_worker_looks_up_qualified_index_by_sql_identity(monkeypatch, sql, protected):
    from unittest.mock import MagicMock
    from app.worker import execute_action
    from app.models import TuneRequest
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [{"id": 1}, None]
    target = MagicMock()
    target.execute.return_value.fetchone.side_effect = [
        (False,), ("postgres",), (None,), ("public", "orders", protected, False, False),
    ]
    connection = MagicMock()
    connection.__enter__.return_value = target
    monkeypatch.setattr("app.worker.psycopg.connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr("app.worker.load_sources", lambda path: {"test": {"db_uri": "postgresql://unused"}})
    request = TuneRequest.model_validate({"mode": "execute", "policy": {"rules": [
        {"advisor_id": "Invalid or unusable indexes", "action_type": "DROP_INDEX"}]}})
    rec = {**recommendation(sql), "object_name": "public.idx", "advisor_id": "Invalid or unusable indexes",
           "id": 1, "run_id": "snapshot", "action_fingerprint": "fingerprint", "database_name": "postgres"}
    status = execute_action(Settings(database_url="postgresql://unused", api_key="test"), conn,
                            {"id": 1, "target_id": "test"}, request, rec, request.policy.rules[0])
    assert status == ("failed" if protected else "succeeded")
    lookup = next(call for call in target.execute.call_args_list if "JOIN pg_index" in call.args[0])
    assert lookup.args[1] == ("public", "idx")
    attempted = [call.args[0] for call in target.execute.call_args_list if call.args[0].startswith(("DROP INDEX", "REINDEX INDEX"))]
    assert bool(attempted) is not protected
