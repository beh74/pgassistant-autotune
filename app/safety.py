"""Validate the entire recommendation before executing any of its statements."""
from pglast import ast, parse_sql
from pglast.stream import RawStream

from app.models import Policy

ALLOWED_SETTINGS = {
    "autovacuum_enabled", "autovacuum_vacuum_threshold", "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_threshold", "autovacuum_analyze_scale_factor",
    "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_cost_limit",
    "autovacuum_vacuum_insert_threshold", "autovacuum_vacuum_insert_scale_factor",
}


GLOBAL_AUTOVACUUM_SETTINGS = {
    "autovacuum", "autovacuum_max_workers", "autovacuum_naptime",
    "autovacuum_vacuum_scale_factor", "autovacuum_vacuum_threshold",
    "autovacuum_analyze_scale_factor", "autovacuum_analyze_threshold",
    "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_cost_limit",
    "log_autovacuum_min_duration",
}


def prepare_configuration(rec: dict) -> list[str]:
    if rec.get("source") != "autovacuum":
        raise UnsupportedAction("Configuration API provider is not implemented")
    if rec.get("requires_restart"):
        raise UnsupportedAction("Configuration requires a restart")
    statements = [raw.stmt for raw in parse_sql(rec.get("recommendation_sql") or "")]
    if not statements:
        raise UnsupportedAction("Recommendation has no executable SQL")
    # The collector emits one parameter recommendation at a time.
    if len(statements) != 1 or not isinstance(statements[0], ast.AlterSystemStmt):
        raise ValueError("Expected one ALTER SYSTEM SET")
    stmt = statements[0]
    setting = stmt.setstmt
    if (setting.name not in GLOBAL_AUTOVACUUM_SETTINGS
            or setting.name != rec.get("advisor_id")
            or setting.kind.name != "VAR_SET_VALUE"
            or len(setting.args or ()) != 1
            or not isinstance(setting.args[0], ast.A_Const)
            or setting.args[0].isnull):
        raise ValueError("Unapproved or mismatched configuration setting")
    return ["ALTER SYSTEM " + RawStream()(setting) + ";", "SELECT pg_catalog.pg_reload_conf();"]


class UnsupportedAction(ValueError):
    pass


def matching_rule(rec: dict, policy: Policy):
    if policy.team is not None and rec.get("team") != policy.team:
        return None
    for rule in policy.rules:
        if rec.get("advisor_id") != rule.advisor_id:
            continue
        if rule.source and rule.source != rec.get("source"):
            continue
        if rule.teams and rec.get("team") not in rule.teams:
            continue
        if rule.action_type == "AUTOVACUUM_TABLE_SETTINGS":
            if rec.get("source") != "autovacuum" or rec.get("action_type") not in {"ANALYZE", "VACUUM"}:
                continue
        elif rec.get("action_type") != rule.action_type:
            continue
        if rule.schemas and rec.get("schema_name") not in rule.schemas:
            continue
        if rule.tables and rec.get("table_name") not in rule.tables:
            continue
        if (rec.get("confidence") or 0) < rule.min_confidence:
            continue
        return rule
    return None


def index_target(stmt) -> tuple[str, str]:
    """Return the validated SQL index identity, independent of display metadata."""
    if isinstance(stmt, ast.ReindexStmt):
        return stmt.relation.schemaname, stmt.relation.relname
    return tuple(part.sval for part in stmt.objects[0])


def index_object_matches(relation, object_name: str | None) -> bool:
    if object_name == relation.relname:
        return True
    # Collector metadata may be schema-qualified and quote identifiers as needed.
    # Parse it as an identifier, preserving case, embedded dots and escaped quotes.
    try:
        parsed = parse_sql("DROP INDEX " + (object_name or ""))
        if len(parsed) != 1:
            return False
        stmt = parsed[0].stmt
        return (isinstance(stmt, ast.DropStmt) and stmt.removeType.name == "OBJECT_INDEX"
                and len(stmt.objects) == 1 and len(stmt.objects[0]) == 2
                and index_target(stmt) == (relation.schemaname, relation.relname))
    except Exception:
        return False


def _prepare_statements(rec: dict, kind: str) -> list[str]:
    if kind == "CONFIG_CHANGE":
        return prepare_configuration(rec)
    if rec.get("requires_restart"):
        raise UnsupportedAction("Restart requires a configuration provider")
    statements = [raw.stmt for raw in parse_sql(rec.get("recommendation_sql") or "")]
    if not statements:
        raise UnsupportedAction("Recommendation has no executable SQL")

    def check_relation(relation, *, index=False):
        if not relation.schemaname or relation.schemaname != rec.get("schema_name"):
            raise ValueError("SQL schema does not match recommendation")
        if index:
            if not index_object_matches(relation, rec.get("object_name")):
                raise ValueError("Index does not match recommended object")
        elif relation.relname != rec.get("table_name"):
            raise ValueError("Table does not match recommendation")

    if kind == "CREATE_INDEX":
        if len(statements) != 1 or not isinstance(statements[0], ast.IndexStmt):
            raise ValueError("Expected one CREATE INDEX")
        stmt = statements[0]
        check_relation(stmt.relation)
        # Preserve the advisor definition: expressions, predicates, INCLUDE and method.
        if not stmt.idxname:
            raise ValueError("Index must have a stable name")
        stmt.concurrent = True
        stmt.if_not_exists = True
        return [RawStream()(stmt) + ";"]

    if kind == "DROP_INDEX":
        if len(statements) != 1:
            raise ValueError("Expected one index maintenance statement")
        stmt = statements[0]
        if isinstance(stmt, ast.ReindexStmt) and rec.get("advisor_id") == "Invalid or unusable indexes":
            if stmt.kind.name != "REINDEX_OBJECT_INDEX":
                raise ValueError("Only the recommended index can be rebuilt")
            check_relation(stmt.relation, index=True)
            return [RawStream()(stmt) + ";"]
        if not isinstance(stmt, ast.DropStmt) or stmt.removeType.name != "OBJECT_INDEX" or stmt.behavior.name != "DROP_RESTRICT":
            raise ValueError("Only DROP INDEX without CASCADE is permitted")
        if len(stmt.objects) != 1 or len(stmt.objects[0]) != 2:
            raise ValueError("Expected one schema-qualified index")
        schema, name = (part.sval for part in stmt.objects[0])
        check_relation(ast.RangeVar(schemaname=schema, relname=name), index=True)
        stmt.concurrent, stmt.missing_ok = True, True
        return [RawStream()(stmt) + ";"]

    if kind not in {"ANALYZE", "VACUUM", "AUTOVACUUM_TABLE_SETTINGS"}:
        raise UnsupportedAction("No executor for this recommendation type")
    bundle = rec.get("source") == "autovacuum"
    selected = []
    for stmt in statements:
        if isinstance(stmt, ast.VacuumStmt):
            if not stmt.rels:
                raise ValueError("Database-wide maintenance is not permitted")
            for relation in stmt.rels:
                check_relation(relation.relation)
            if kind == "AUTOVACUUM_TABLE_SETTINGS":
                continue
            if not bundle and (stmt.is_vacuumcmd != (kind == "VACUUM")):
                raise ValueError("Maintenance command does not match action type")
        elif bundle or kind == "AUTOVACUUM_TABLE_SETTINGS":
            if not isinstance(stmt, ast.AlterTableStmt) or stmt.objtype.name != "OBJECT_TABLE":
                raise ValueError("Unexpected SQL in autovacuum recommendation")
            check_relation(stmt.relation)
            for command in stmt.cmds:
                if command.subtype.name != "AT_SetRelOptions":
                    raise ValueError("Only autovacuum table settings are permitted")
                for option in command.def_:
                    if option.defname not in ALLOWED_SETTINGS or option.defnamespace:
                        raise ValueError("Unapproved table option")
        else:
            raise ValueError("Unexpected maintenance statement")
        selected.append(RawStream()(stmt) + ";")
    if not selected:
        raise UnsupportedAction("No executable statements for the selected operation")
    return selected


class ExcludedCommand(ValueError):
    pass


def statement_commands(stmt) -> set[str]:
    """Command categories come from the PostgreSQL AST, never substring matching."""
    if isinstance(stmt, ast.IndexStmt):
        return {"CREATE_INDEX"}
    if isinstance(stmt, ast.DropStmt) and stmt.removeType.name == "OBJECT_INDEX":
        return {"DROP_INDEX"}
    if isinstance(stmt, ast.ReindexStmt):
        return {"REINDEX"}
    if isinstance(stmt, ast.AlterTableStmt):
        return {"ALTER_TABLE"}
    if isinstance(stmt, ast.AlterSystemStmt):
        return {"ALTER_SYSTEM"}
    if isinstance(stmt, ast.VacuumStmt):
        commands = {"VACUUM" if stmt.is_vacuumcmd else "ANALYZE"}
        if stmt.is_vacuumcmd:
            for option in stmt.options or ():
                if option.defname == "analyze":
                    value = option.arg
                    disabled = (isinstance(value, ast.String) and value.sval.lower() in {"false", "off", "no", "0"}
                                or isinstance(value, ast.Integer) and value.ival == 0)
                    if not disabled:
                        commands.add("ANALYZE")
        return commands
    # The only SELECT admitted by the executor is its generated reload step.
    if isinstance(stmt, ast.SelectStmt):
        return {"RELOAD_CONFIGURATION"}
    raise ValueError("Unrecognized SQL command")


def prepare_statements(rec: dict, kind: str, exclude_commands=()) -> list[str]:
    statements = _prepare_statements(rec, kind)
    if exclude_commands:
        # Inspect the original bundle too: never execute a partial recommendation.
        nodes = [raw.stmt for raw in parse_sql(rec.get("recommendation_sql") or "")]
        nodes.extend(raw.stmt for sql in statements for raw in parse_sql(sql))
        commands = set().union(*(statement_commands(node) for node in nodes))
        excluded = commands.intersection(exclude_commands)
        if excluded:
            raise ExcludedCommand("SQL commands excluded by policy: " + ", ".join(sorted(excluded)))
    return statements


def prepare_sql(rec: dict, kind: str) -> str:
    return "\n".join(prepare_statements(rec, kind))
