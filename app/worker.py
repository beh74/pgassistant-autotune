"""Durable PostgreSQL queue. Run separately from the HTTP service."""
import logging
import sys
import time
from datetime import timedelta

import psycopg
from pglast import ast, parse_sql
from psycopg.types.json import Jsonb

from app.db import connect
from app.models import TuneRequest
from app.safety import matching_rule, prepare_statements, UnsupportedAction, ExcludedCommand, index_target
from app.providers import execution_backend
from app.settings import Settings
from app.sources import execution_uri, load_sources


logger = logging.getLogger(__name__)


class IncompleteSnapshot(ValueError):
    """Safe diagnostic containing snapshot metadata, never raw advisor errors."""


def select_snapshot(conn, run, request):
    snapshot = conn.execute(
        """SELECT run_id, collected_at, status, advisor_errors FROM pga_executive_plan_snapshot
        WHERE target_id=%s AND collected_at <= %s AND collected_at >= %s
        ORDER BY collected_at DESC, run_id DESC LIMIT 1""",
        (run["target_id"], run["cutoff"], run["cutoff"] - timedelta(seconds=request.selection.max_snapshot_age_seconds)),
    ).fetchone()
    if snapshot is None:
        return None, []
    if snapshot["status"] != "ok" or snapshot["advisor_errors"]:
        errors = snapshot["advisor_errors"] or []
        sources = sorted({str(item.get("source", "unknown")) for item in errors if isinstance(item, dict)})
        reason = (f"Selected snapshot is incomplete: snapshot_id={snapshot['run_id']} "
                  f"status={snapshot['status']!r} advisor_error_count={len(errors)} "
                  f"advisor_sources={sources!r}; refusing an older silent fallback")
        logger.warning("run_id=%s target_id=%r %s", run["id"], run["target_id"], reason)
        raise IncompleteSnapshot(reason)
    recommendations = conn.execute(
        "SELECT * FROM pga_executive_plan_recommendation_snapshot WHERE run_id=%s AND target_id=%s ORDER BY id",
        (snapshot["run_id"], run["target_id"]),
    ).fetchall()
    return snapshot, recommendations


def execute_action(settings, conn, run, request, rec, rule):
    backend = execution_backend(rule.action_type, rec.get("source"))
    statements = []
    try:
        statements = prepare_statements(rec, rule.action_type, request.policy.exclude_commands)
        status, reason = "planned", None
    except ExcludedCommand as exc:
        status, reason = "skipped", str(exc)
    except UnsupportedAction as exc:
        status = "deferred" if rule.action_type == "CONFIG_CHANGE" else "unsupported"
        reason = str(exc)
    except Exception:
        status, reason = "rejected", "Unsupported SQL or mismatched recommendation object"
    sql = "\n".join(statements)
    # Maintenance can recur on a later snapshot, never repeatedly on the same one.
    recurring = rule.action_type in {"ANALYZE", "VACUUM", "AUTOVACUUM_TABLE_SETTINGS"}
    execution_key = (f"{rec['action_fingerprint']}:{rule.action_type}:{rec['run_id']}"
                     if recurring else rec["action_fingerprint"])
    snapshot = {key: (value.isoformat() if hasattr(value, "isoformat") else str(value) if key == "run_id" else value) for key, value in rec.items()}
    action_id = conn.execute(
        """INSERT INTO autotune.actions
        (run_id,target_id,recommendation_id,action_fingerprint,advisor_id,recommendation,
         proposed_sql,status,error,action_type,execution_backend,execution_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (run["id"], run["target_id"], rec["id"], rec["action_fingerprint"], rec["advisor_id"], Jsonb(snapshot),
         sql or rec.get("recommendation_sql"), status, reason, rule.action_type, backend, execution_key),
    ).fetchone()["id"]
    log = logger
    context = f"run_id={run['id']} target_id={run['target_id']!r} action_id={action_id}"
    log.info("%s action prepared advisor_id=%r action_type=%s mode=%s status=%s steps=%s schema=%r table=%r object=%r reason=%r",
             context, rec["advisor_id"], rule.action_type, request.mode, status, len(statements),
             rec.get("schema_name"), rec.get("table_name"), rec.get("object_name"), reason)
    for number, statement in enumerate(statements, 1):
        conn.execute("INSERT INTO autotune.action_steps(action_id,step_number,sql) VALUES (%s,%s,%s)", (action_id, number, statement))
    if status != "planned" or request.mode == "dry_run":
        return status
    duplicate = conn.execute(
        """SELECT 1 FROM autotune.actions WHERE target_id=%s AND
        ((execution_key=%s AND status IN ('running','succeeded','failed','unknown'))
        OR (action_fingerprint=%s AND status IN ('running','failed','unknown')))""",
        (run["target_id"], execution_key, rec["action_fingerprint"]),
    ).fetchone()
    if duplicate:
        conn.execute("UPDATE autotune.actions SET status='skipped',error='Previously attempted action; review required' WHERE id=%s", (action_id,))
        conn.execute("UPDATE autotune.action_steps SET status='skipped' WHERE action_id=%s", (action_id,))
        log.info("%s action finished status=skipped reason=previously_attempted", context)
        return "skipped"
    conn.execute("UPDATE autotune.actions SET status='running',started_at=now() WHERE id=%s", (action_id,))
    log.info("%s action started backend=%s", context, backend)
    started = time.monotonic()
    sent = False
    current_step = None
    step_started = None
    attempted = []
    try:
        source = load_sources(settings.sources_path)[run["target_id"]]
        if not source.get("enabled", True):
            raise ValueError("Disabled target")
        log.info("%s connecting to target", context)
        with psycopg.connect(execution_uri(source), autocommit=True, connect_timeout=10) as target:
            target.execute("SELECT set_config('statement_timeout', %s, false)", (str(request.policy.statement_timeout_seconds * 1000),))
            target.execute("SELECT set_config('lock_timeout', %s, false)", (str(request.policy.lock_timeout_seconds * 1000),))
            if target.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                raise ValueError("Read-only replica")
            current_db = target.execute("SELECT current_database()").fetchone()[0]
            if rec.get("database_name") and current_db != rec["database_name"]:
                raise ValueError("Target database mismatch")
            if rule.action_type == "CONFIG_CHANGE":
                parameter = parse_sql(statements[0])[0].stmt.setstmt.name
                before = target.execute(
                    "SELECT name,setting,unit,context,pending_restart FROM pg_settings WHERE name=%s",
                    (parameter,),
                ).fetchone()
                if before is None:
                    raise ValueError("Unknown configuration parameter")
                if before[3] == "postmaster":
                    raise UnsupportedAction("Configuration requires a restart")
                conn.execute("UPDATE autotune.actions SET target_before=%s WHERE id=%s",
                             (Jsonb(dict(zip(("name", "setting", "unit", "context", "pending_restart"), before))), action_id))
            else:
                before = target.execute(
                    """SELECT c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p','m')""",
                    (rec["schema_name"], rec["table_name"]),
                ).fetchone()
                if before is None:
                    raise ValueError("Recommended table no longer exists")
                indexes = target.execute(
                    "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s",
                    (rec["schema_name"], rec["table_name"]),
                ).fetchall()
                conn.execute("UPDATE autotune.actions SET target_before=%s WHERE id=%s", (Jsonb({"reloptions": before[0], "indexes": indexes}), action_id))
            stmt = parse_sql(statements[0])[0].stmt
            skip_reason = None
            if rule.action_type == "CREATE_INDEX":
                existing = target.execute(
                    "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s",
                    (rec["schema_name"], stmt.idxname),
                ).fetchone()
                if existing:
                    skip_reason = "Index name already exists; review definition and validity"
            elif rule.action_type == "DROP_INDEX":
                index = target.execute(
                    """SELECT tn.nspname,t.relname,i.indisprimary,i.indisreplident,
                    EXISTS(SELECT 1 FROM pg_constraint WHERE conindid=c.oid)
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_class t ON t.oid=i.indrelid
                    JOIN pg_namespace tn ON tn.oid=t.relnamespace
                    WHERE n.nspname=%s AND c.relname=%s""",
                    index_target(stmt),
                ).fetchone()
                if index is None:
                    skip_reason = "Index no longer exists"
                elif index[:2] != (rec["schema_name"], rec["table_name"]):
                    raise ValueError("Index belongs to another table")
                elif isinstance(stmt, ast.DropStmt) and any(index[2:]):
                    raise ValueError("Constraint or replica identity index cannot be dropped")
            if skip_reason:
                status, error = "skipped", skip_reason
                conn.execute("UPDATE autotune.action_steps SET status='skipped' WHERE action_id=%s", (action_id,))
            else:
                for current_step, statement in enumerate(statements, 1):
                    step_started = time.monotonic()
                    conn.execute("UPDATE autotune.action_steps SET status='running',started_at=now() WHERE action_id=%s AND step_number=%s", (action_id, current_step))
                    attempted.append(statement)
                    conn.execute("UPDATE autotune.actions SET executed_sql=%s WHERE id=%s", ("\n".join(attempted), action_id))
                    log.info("%s step started step=%s/%s command=%s", context,
                             current_step, len(statements), type(parse_sql(statement)[0].stmt).__name__)
                    sent = True
                    # Separate autocommit statements: VACUUM and concurrent DDL must
                    # not be sent as one multi-statement implicit transaction.
                    result = target.execute(statement)
                    if statement == "SELECT pg_catalog.pg_reload_conf();" and not result.fetchone()[0]:
                        raise ValueError("PostgreSQL configuration reload failed")
                    conn.execute("UPDATE autotune.action_steps SET status='succeeded',finished_at=now(),execution_ms=%s WHERE action_id=%s AND step_number=%s", ((time.monotonic()-step_started)*1000, action_id, current_step))
                    log.info("%s step finished step=%s/%s status=succeeded duration_ms=%.1f",
                             context, current_step, len(statements), (time.monotonic()-step_started)*1000)
                    sent = False
                    current_step = None
                status, error = "succeeded", None
    except Exception as exc:
        status = "deferred" if isinstance(exc, UnsupportedAction) else "unknown" if sent and isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)) else "failed"
        error = f"{type(exc).__name__}; SQLSTATE={getattr(exc, 'sqlstate', None) or 'n/a'}"
        log.log(logging.WARNING if status == "deferred" else logging.ERROR,
                "%s execution stopped step=%s status=%s error=%s", context, current_step, status, error)
        if current_step is not None:
            conn.execute("UPDATE autotune.action_steps SET status=%s,error=%s,finished_at=now(),execution_ms=%s WHERE action_id=%s AND step_number=%s", (status, error, (time.monotonic()-step_started)*1000, action_id, current_step))
        conn.execute("UPDATE autotune.action_steps SET status='not_run' WHERE action_id=%s AND status='planned'", (action_id,))
    conn.execute(
        "UPDATE autotune.actions SET status=%s,error=%s,finished_at=now(),execution_ms=%s WHERE id=%s",
        (status, error, (time.monotonic() - started) * 1000, action_id),
    )
    log.info("%s action finished status=%s duration_ms=%.1f reason=%r",
             context, status, (time.monotonic()-started)*1000, error)
    return status


def process(settings, conn, run):
    request = TuneRequest.model_validate(run["request"])
    if not request.policy.rules:
        logger.info("run_id=%s target_id=%r no authorized rules; no_matches", run["id"], run["target_id"])
        return "no_matches"
    with connect(settings, collector=True) as collector:
        snapshot, recommendations = select_snapshot(collector, run, request)
    if snapshot is None:
        logger.info("run_id=%s target_id=%r no eligible snapshot", run["id"], run["target_id"])
        return "no_snapshot"
    conn.execute("UPDATE autotune.runs SET snapshot_run_id=%s,snapshot_collected_at=%s WHERE id=%s", (snapshot["run_id"], snapshot["collected_at"], run["id"]))
    logger.info("run_id=%s target_id=%r snapshot selected snapshot_id=%s recommendations=%s mode=%s",
                run["id"], run["target_id"], snapshot["run_id"], len(recommendations), request.mode)
    outcomes = []
    for rec in recommendations:
        rule = matching_rule(rec, request.policy)
        if rule:
            outcomes.append(execute_action(settings, conn, run, request, rec, rule))
            if outcomes[-1] in ("failed", "unknown") or len(outcomes) >= request.policy.max_actions:
                logger.info("run_id=%s target_id=%r stopping action selection reason=%s actions=%s",
                            run["id"], run["target_id"],
                            "execution_error" if outcomes[-1] in ("failed", "unknown") else "max_actions", len(outcomes))
                break
    logger.info("run_id=%s target_id=%r actions processed matched=%s recommendations=%s",
                run["id"], run["target_id"], len(outcomes), len(recommendations))
    if any(status in ("failed", "unknown", "rejected", "unsupported", "deferred") for status in outcomes):
        return "needs_review"
    return "completed" if outcomes else "no_matches"


def tick(settings):
    # Session advisory lock prevents concurrent execution on the same target.
    with connect(settings) as conn:
        conn.autocommit = True
        targets = [key for key, source in load_sources(settings.sources_path).items() if source.get("enabled", True)]
        candidates = conn.execute("SELECT * FROM autotune.runs WHERE target_id=ANY(%s) AND status IN ('queued','running') ORDER BY created_at LIMIT 100", (targets,)).fetchall()
        for run in candidates:
            key = f"autotune:{run['target_id']}"
            if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (key,)).fetchone()["locked"]:
                logger.debug("run_id=%s target_id=%r target locked by another worker", run["id"], run["target_id"])
                continue
            try:
                current = conn.execute("SELECT status FROM autotune.runs WHERE id=%s", (run["id"],)).fetchone()
                if current["status"] == "running":
                    logger.warning("run_id=%s target_id=%r interrupted run detected; marking needs_review",
                                   run["id"], run["target_id"])
                    # Owning worker disappeared. Never replay a potentially applied DDL.
                    interrupt_steps(conn, run["id"])
                    conn.execute("UPDATE autotune.actions SET status='unknown',finished_at=now(),error='Worker interrupted; reconcile target state' WHERE run_id=%s AND status='running'", (run["id"],))
                    conn.execute("UPDATE autotune.runs SET status='needs_review',finished_at=now(),error='Worker interrupted' WHERE id=%s", (run["id"],))
                    return True
                if current["status"] != "queued":
                    continue
                conn.execute("UPDATE autotune.runs SET status='running',started_at=now() WHERE id=%s", (run["id"],))
                logger.info("run_id=%s target_id=%r run started", run["id"], run["target_id"])
                try:
                    status, error = process(settings, conn, run), None
                except Exception as exc:
                    status, error = "needs_review", str(exc) if isinstance(exc, IncompleteSnapshot) else type(exc).__name__
                    logger.error("run_id=%s target_id=%r run failed error=%s sqlstate=%s",
                                 run["id"], run["target_id"], error, getattr(exc, "sqlstate", None) or "n/a")
                    interrupt_steps(conn, run["id"])
                    conn.execute("UPDATE autotune.actions SET status='unknown',finished_at=now(),error='Execution interrupted; reconcile target state' WHERE run_id=%s AND status='running'", (run["id"],))
                conn.execute("UPDATE autotune.runs SET status=%s,error=%s,finished_at=now() WHERE id=%s", (status, error, run["id"]))
                logger.info("run_id=%s target_id=%r run finished status=%s", run["id"], run["target_id"], status)
                return True
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
    return False


def interrupt_steps(conn, run_id):
    conn.execute("""UPDATE autotune.action_steps SET status='unknown',finished_at=now(),error='Worker interrupted; reconcile target state'
        WHERE status='running' AND action_id IN (SELECT id FROM autotune.actions WHERE run_id=%s)""", (run_id,))
    conn.execute("""UPDATE autotune.action_steps SET status='not_run'
        WHERE status='planned' AND action_id IN (SELECT id FROM autotune.actions WHERE run_id=%s)""", (run_id,))


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    try:
        settings = Settings()
        logger.info("Worker started poll_seconds=%s", settings.poll_seconds)
        while True:
            if not tick(settings):
                time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        logger.info("Worker stopped")
    except Exception as exc:
        # Connection errors can contain credentials; never log raw exceptions.
        logger.critical("Worker stopped error=%s sqlstate=%s",
                        type(exc).__name__, getattr(exc, "sqlstate", None) or "n/a")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
