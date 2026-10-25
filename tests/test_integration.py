"""Run only against a disposable database: AUTOTUNE_TEST_DSN=... pytest."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import connect, migrate
from app.main import create_app
from app.settings import Settings
from app.worker import tick

DSN = os.getenv("AUTOTUNE_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="Disposable PostgreSQL DSN not supplied")


@pytest.fixture
def service(tmp_path):
    sources = tmp_path / "sources.yaml"
    sources.write_text(f"sources:\n  - id: test-target\n    db_uri: postgresql://unused\n    autotune_uri: {DSN}\n")
    settings = Settings(database_url=DSN, api_key="test-key", sources_path=str(sources))
    # This fixture must never be pointed at the real collector database.
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS autotune CASCADE")
        conn.execute("DROP TABLE IF EXISTS pga_executive_plan_recommendation_snapshot, pga_executive_plan_snapshot, autotune_test_table CASCADE")
        conn.execute("CREATE TABLE autotune_test_table (id int, parent_id int)")
        conn.execute("CREATE TABLE pga_executive_plan_snapshot (run_id uuid PRIMARY KEY, target_id text, collected_at timestamptz, status text, advisor_errors jsonb DEFAULT '[]')")
        conn.execute("""CREATE TABLE pga_executive_plan_recommendation_snapshot (
            id bigint GENERATED ALWAYS AS IDENTITY, run_id uuid, target_id text,
            action_fingerprint text, advisor_id text, action_type text, source text,
            schema_name text, table_name text, database_name text, confidence int,
            object_name text, recommendation_sql text, requires_restart boolean DEFAULT false)""")
    migrate(settings)
    migrate(settings)
    return settings, TestClient(create_app(settings))


def seed(settings, *, hours=2, advisor="missing_useful_foreign_key_indexes", action="CREATE_INDEX", source="global_advisor", sql="CREATE INDEX autotune_test_idx ON public.autotune_test_table(parent_id)", status="ok", object_name="autotune_test_table"):
    snapshot_id = uuid4()
    with connect(settings) as conn:
        conn.execute("INSERT INTO pga_executive_plan_snapshot(run_id,target_id,collected_at,status) VALUES (%s,'test-target',%s,%s)", (snapshot_id, datetime.now(timezone.utc) - timedelta(hours=hours), status))
        conn.execute("""INSERT INTO pga_executive_plan_recommendation_snapshot
        (run_id,target_id,action_fingerprint,advisor_id,action_type,source,schema_name,table_name,database_name,confidence,object_name,recommendation_sql)
        VALUES (%s,'test-target',%s,%s,%s,%s,'public','autotune_test_table','postgres',85,%s,%s)""", (snapshot_id, advisor + sql, advisor, action, source, object_name, sql))
    return snapshot_id


def submit(client, *, mode="execute", advisor="missing_useful_foreign_key_indexes", kind="CREATE_INDEX", lookback=3600):
    response = client.post("/api/v1/autotune-targetid", headers={"X-API-Key": "test-key"}, json={
        "target_id": "test-target", "mode": mode,
        "selection": {"lookback_seconds": lookback},
        "policy": {"rules": [{"advisor_id": advisor, "action_type": kind}]},
    })
    assert response.status_code == 202, response.text
    return response.json()["runs"][0]["run_id"]


def detail(client, run_id):
    response = client.get(f"/api/v1/runs/{run_id}", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.text
    return response.json()


def test_temporal_execution_and_duplicate_prevention(service):
    settings, client = service
    older = seed(settings)
    seed(settings, hours=0, sql="CREATE INDEX wrong_snapshot_idx ON public.autotune_test_table(id)")
    run_id = submit(client)
    assert tick(settings)
    result = detail(client, run_id)
    assert result["status"] == "completed", result
    assert result["snapshot_run_id"] == str(older)
    action = result["actions"][0]
    assert action["status"] == "succeeded", action
    assert action["execution_ms"] >= 0
    assert "CONCURRENTLY" in action["executed_sql"]
    assert action["target_before"] is not None
    second = submit(client)
    tick(settings)
    assert detail(client, second)["actions"][0]["status"] == "skipped"
    with connect(settings) as conn:
        assert conn.execute("SELECT to_regclass('public.autotune_test_idx') AS idx").fetchone()["idx"]
        assert conn.execute("SELECT to_regclass('public.wrong_snapshot_idx') AS idx").fetchone()["idx"] is None


def test_dry_run_and_no_snapshot(service):
    settings, client = service
    run_id = submit(client)
    tick(settings)
    assert detail(client, run_id)["status"] == "no_snapshot"
    seed(settings)
    run_id = submit(client, mode="dry_run")
    tick(settings)
    result = detail(client, run_id)
    assert result["actions"][0]["status"] == "planned"
    assert result["actions"][0]["executed_sql"] is None


def test_autovacuum_settings_only(service):
    settings, client = service
    seed(settings, advisor="table_autovacuum", action="VACUUM", source="autovacuum", sql="ALTER TABLE public.autotune_test_table SET (autovacuum_vacuum_scale_factor=0.05); ANALYZE public.autotune_test_table; VACUUM public.autotune_test_table;")
    run_id = submit(client, advisor="table_autovacuum", kind="AUTOVACUUM_TABLE_SETTINGS")
    tick(settings)
    result = detail(client, run_id)
    assert result["status"] == "completed", result
    assert "ANALYZE" not in result["actions"][0]["executed_sql"]
    with connect(settings) as conn:
        options = conn.execute("SELECT reloptions FROM pg_class WHERE oid='public.autotune_test_table'::regclass").fetchone()["reloptions"]
    assert "autovacuum_vacuum_scale_factor=0.05" in options


def test_interruption_not_replayed(service):
    settings, client = service
    seed(settings)
    run_id = submit(client)
    with connect(settings) as conn:
        conn.execute("UPDATE autotune.runs SET status='running' WHERE id=%s", (run_id,))
    tick(settings)
    assert detail(client, run_id)["status"] == "needs_review"
    assert not tick(settings)


def test_sql_failure_and_partial_snapshot(service):
    settings, client = service
    seed(settings, sql="CREATE INDEX bad_idx ON public.autotune_test_table(missing_column)")
    run_id = submit(client)
    tick(settings)
    result = detail(client, run_id)
    assert result["status"] == "needs_review"
    assert result["actions"][0]["status"] == "failed"
    assert result["actions"][0]["executed_sql"]
    seed(settings, hours=1.5, status="partial")
    run_id = submit(client)
    tick(settings)
    result = detail(client, run_id)
    assert result["status"] == "needs_review"
    assert not result["actions"]


def test_all_endpoint_override_and_unknown_target(service):
    settings, client = service
    seed(settings)
    response = client.post("/api/v1/autotune-all", headers={"X-API-Key": "test-key"}, json={
        "policy": {"rules": [{"advisor_id": "not_selected", "action_type": "CREATE_INDEX"}]},
        "target_policies": {"test-target": {"rules": [{"advisor_id": "missing_useful_foreign_key_indexes", "action_type": "CREATE_INDEX"}]}},
    })
    assert response.status_code == 202
    run_id = response.json()["runs"][0]["run_id"]
    tick(settings)
    assert detail(client, run_id)["actions"][0]["status"] == "planned"
    response = client.post("/api/v1/autotune-targetid", headers={"X-API-Key": "test-key"}, json={
        "target_id": "unknown", "policy": {"rules": [{"advisor_id": "fk", "action_type": "CREATE_INDEX"}]},
    })
    assert response.status_code == 404


def test_target_lock_and_existing_index(service):
    settings, client = service
    seed(settings)
    run_id = submit(client)
    with connect(settings) as other_worker:
        other_worker.execute("SELECT pg_advisory_lock(hashtextextended('autotune:test-target',0))")
        assert not tick(settings)
        assert detail(client, run_id)["status"] == "queued"
    with connect(settings) as conn:
        conn.execute("CREATE INDEX autotune_test_idx ON public.autotune_test_table(id)")
    tick(settings)
    action = detail(client, run_id)["actions"][0]
    assert action["status"] == "skipped"
    assert action["executed_sql"] is None


def test_snapshot_too_old_and_rule_mismatch(service):
    settings, client = service
    seed(settings, hours=50)
    run_id = submit(client)
    tick(settings)
    assert detail(client, run_id)["status"] == "no_snapshot"
    seed(settings, advisor="order_by_index")
    run_id = submit(client)
    tick(settings)
    assert detail(client, run_id)["status"] == "no_matches"


def test_full_autovacuum_script_and_recurrence(service):
    settings, client = service
    script = "ALTER TABLE public.autotune_test_table SET (autovacuum_vacuum_scale_factor=0.02); ANALYZE public.autotune_test_table; VACUUM (ANALYZE) public.autotune_test_table;"
    seed(settings, advisor="stale_vacuum", action="VACUUM", source="autovacuum", sql=script)
    run_id = submit(client, advisor="stale_vacuum", kind="VACUUM")
    tick(settings)
    result = detail(client, run_id)
    assert result["status"] == "completed", result
    assert [s["status"] for s in result["actions"][0]["steps"]] == ["succeeded"] * 3
    assert "VACUUM" in result["actions"][0]["executed_sql"]
    second = submit(client, advisor="stale_vacuum", kind="VACUUM")
    tick(settings)
    assert detail(client, second)["actions"][0]["status"] == "skipped"
    seed(settings, hours=1.5, advisor="stale_vacuum", action="VACUUM", source="autovacuum", sql=script)
    third = submit(client, advisor="stale_vacuum", kind="VACUUM")
    tick(settings)
    assert detail(client, third)["actions"][0]["status"] == "succeeded"


@pytest.mark.parametrize("kind,sql", [("ANALYZE", "ANALYZE public.autotune_test_table"), ("VACUUM", "VACUUM (ANALYZE) public.autotune_test_table")])
def test_global_maintenance(service, kind, sql):
    settings, client = service
    seed(settings, advisor="maintenance", action=kind, sql=sql)
    run_id = submit(client, advisor="maintenance", kind=kind)
    tick(settings)
    assert detail(client, run_id)["actions"][0]["status"] == "succeeded"


def test_drop_index_and_constraint_protection(service):
    settings, client = service
    with connect(settings) as conn:
        conn.execute("CREATE INDEX unused_test_idx ON autotune_test_table(parent_id)")
    seed(settings, advisor="unused_non_constraint_indexes", action="DROP_INDEX", sql="DROP INDEX public.unused_test_idx", object_name="unused_test_idx")
    run_id = submit(client, advisor="unused_non_constraint_indexes", kind="DROP_INDEX")
    tick(settings)
    assert detail(client, run_id)["actions"][0]["status"] == "succeeded"
    with connect(settings) as conn:
        assert conn.execute("SELECT to_regclass('public.unused_test_idx') AS idx").fetchone()["idx"] is None
        conn.execute("ALTER TABLE autotune_test_table ADD CONSTRAINT protected_test_pk PRIMARY KEY(id)")
    seed(settings, hours=1.5, advisor="unused_non_constraint_indexes", action="DROP_INDEX", sql="DROP INDEX public.protected_test_pk", object_name="protected_test_pk")
    run_id = submit(client, advisor="unused_non_constraint_indexes", kind="DROP_INDEX")
    tick(settings)
    result = detail(client, run_id)
    assert result["actions"][0]["status"] == "failed"
    assert result["actions"][0]["executed_sql"] is None


def test_reindex_advisor_and_expression_index(service):
    settings, client = service
    seed(settings, sql="CREATE INDEX expression_test_idx ON public.autotune_test_table ((id + 1)) WHERE parent_id IS NOT NULL")
    run_id = submit(client)
    tick(settings)
    assert detail(client, run_id)["actions"][0]["status"] == "succeeded"
    seed(settings, hours=1.5, advisor="Invalid or unusable indexes", action="DROP_INDEX", sql="REINDEX INDEX public.expression_test_idx", object_name="expression_test_idx")
    run_id = submit(client, advisor="Invalid or unusable indexes", kind="DROP_INDEX")
    tick(settings)
    assert detail(client, run_id)["actions"][0]["status"] == "succeeded"


def test_configuration_deferred_and_non_sql_other(service):
    settings, client = service
    seed(settings, advisor="autovacuum_naptime", action="CONFIG_CHANGE", source="parameter_advisor", sql="ALTER SYSTEM SET autovacuum_naptime='10s'")
    run_id = submit(client, advisor="autovacuum_naptime", kind="CONFIG_CHANGE")
    tick(settings)
    action = detail(client, run_id)["actions"][0]
    assert action["status"] == "deferred"
    assert action["execution_backend"] == "configuration_api"
    assert action["executed_sql"] is None and not action["steps"]
    seed(settings, hours=1.5, advisor="PostgreSQL release upgrade", action="OTHER", sql="-- Upgrade PostgreSQL manually")
    run_id = submit(client, advisor="PostgreSQL release upgrade", kind="OTHER")
    tick(settings)
    assert detail(client, run_id)["actions"][0]["status"] == "unsupported"


def test_script_failure_keeps_per_step_audit(service):
    settings, client = service
    seed(settings, advisor="stale_vacuum", action="VACUUM", source="autovacuum", sql="ALTER TABLE public.autotune_test_table SET (autovacuum_vacuum_scale_factor=0.03); ANALYZE public.autotune_test_table(missing_column); VACUUM public.autotune_test_table;")
    run_id = submit(client, advisor="stale_vacuum", kind="VACUUM")
    tick(settings)
    action = detail(client, run_id)["actions"][0]
    assert action["status"] == "failed"
    assert [s["status"] for s in action["steps"]] == ["succeeded", "failed", "not_run"]
    assert "VACUUM public" not in action["executed_sql"]


def test_bigint_identities_and_reinitialization(service):
    settings, client = service
    seed(settings)
    first = submit(client)
    second = submit(client)
    assert isinstance(first, int) and second > first
    tick(settings)
    action = detail(client, first)["actions"][0]
    assert isinstance(action["id"], int)
    assert action["run_id"] == first
    assert all(step["action_id"] == action["id"] for step in action["steps"])
    migrate(settings)
    assert detail(client, first)["actions"][0]["id"] == action["id"]
    with connect(settings) as conn:
        columns = conn.execute("SELECT table_name,column_name,data_type,is_identity,identity_generation FROM information_schema.columns WHERE table_schema='autotune'").fetchall()
    lookup = {(c["table_name"], c["column_name"]): c for c in columns}
    for table in ("runs", "actions"):
        assert lookup[table, "id"]["data_type"] == "bigint"
        assert lookup[table, "id"]["identity_generation"] == "ALWAYS"
    assert lookup["actions", "run_id"]["data_type"] == "bigint"
    assert lookup["action_steps", "action_id"]["data_type"] == "bigint"
    assert lookup["runs", "snapshot_run_id"]["data_type"] == "uuid"


def test_autovacuum_configuration_dry_run(service):
    settings, client = service
    seed(settings, advisor="autovacuum_naptime", action="CONFIG_CHANGE", source="autovacuum",
         sql="ALTER SYSTEM SET autovacuum_naptime='10s'")
    run_id = submit(client, mode="dry_run", advisor="autovacuum_naptime", kind="CONFIG_CHANGE")
    tick(settings)
    action = detail(client, run_id)["actions"][0]
    assert action["status"] == "planned"
    assert action["execution_backend"] == "postgresql"
    assert len(action["steps"]) == 2
    assert action["executed_sql"] is None
