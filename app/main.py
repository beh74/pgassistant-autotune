from datetime import datetime, timezone
from hmac import compare_digest

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Path
from psycopg.types.json import Jsonb

from app.db import connect
from app.models import AllRequest, TargetRequest, TargetReference, TuneRequest
from app.policies import load_default_policy
from app.settings import Settings
from app.sources import load_sources


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    app = FastAPI(title="pgassistant-autotune", version="0.1.0")

    def authenticate(x_api_key: str = Header(default="")):
        if not compare_digest(x_api_key, settings.api_key.get_secret_value()):
            raise HTTPException(401, "Invalid API key")

    protected = [Depends(authenticate)]

    def default_policy():
        try:
            return load_default_policy(settings.policy_path)
        except (ValueError, OSError):
            raise HTTPException(503, "Default policy unavailable or invalid; check AUTOTUNE_POLICY_PATH") from None

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "pgassistant-autotune"}

    def enqueue(request: TuneRequest, target_id: str | None, overrides=None):
        sources = load_sources(settings.sources_path)
        targets = {key: source for key, source in sources.items() if source.get("enabled", True)}
        if target_id:
            if target_id not in targets:
                raise HTTPException(404, "Unknown or disabled target")
            targets = {target_id: targets[target_id]}
        if set(overrides or {}) - targets.keys():
            raise HTTPException(422, "Policy override refers to unknown or disabled target")
        if request.mode == "execute" and any(
            not (source.get("autotune_uri") or source.get("db_uri") or source.get("conn_str")) and any((rule.action_type != "CONFIG_CHANGE" or rule.source in (None, "autovacuum")) for rule in ((overrides or {}).get(key) or request.policy).rules)
            for key, source in targets.items()
        ):
            raise HTTPException(422, "Every selected target needs db_uri, conn_str or autotune_uri for execution")
        cutoff = request.selection.cutoff(datetime.now(timezone.utc))
        result = []
        with connect(settings) as conn:
            for key in targets:
                payload = request.model_dump(mode="json", exclude={"target_id", "target_policies"})
                if overrides and key in overrides:
                    payload["policy"] = overrides[key].model_dump(mode="json")
                run_id = conn.execute(
                    "INSERT INTO autotune.runs(target_id,request,cutoff) VALUES (%s,%s,%s) RETURNING id",
                    (key, Jsonb(payload), cutoff),
                ).fetchone()["id"]
                result.append({"run_id": run_id, "target_id": key, "status": "queued"})
        return {"runs": result}

    @app.post("/api/v1/autotune-all", status_code=202, dependencies=protected)
    def tune_all(request: AllRequest | None = None):
        request = request if request is not None else default_policy()
        return enqueue(request, None, request.target_policies)

    @app.post("/api/v1/autotune-targetid", status_code=202, dependencies=protected)
    def tune_target(request: TargetRequest | TargetReference | None = None, target_id: str | None = Query(default=None, min_length=1)):
        if request is not None:
            if target_id is not None and target_id != request.target_id:
                raise HTTPException(422, "Conflicting target IDs")
            target_id = request.target_id
        if target_id is None:
            raise HTTPException(422, "target_id is required in the query or JSON body")
        if isinstance(request, TargetRequest):
            return enqueue(request, target_id)
        configured = default_policy()
        override = {target_id: configured.target_policies[target_id]} if target_id in configured.target_policies else None
        return enqueue(configured, target_id, override)

    @app.get("/api/v1/runs", dependencies=protected)
    def runs(target_id: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        with connect(settings) as conn:
            return conn.execute(
                "SELECT * FROM autotune.runs WHERE (%s::text IS NULL OR target_id=%s) ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (target_id, target_id, limit, offset),
            ).fetchall()

    @app.get("/api/v1/runs/{run_id}", dependencies=protected)
    def run_detail(run_id: int = Path(ge=1, le=9223372036854775807)):
        with connect(settings) as conn:
            run = conn.execute("SELECT * FROM autotune.runs WHERE id=%s", (run_id,)).fetchone()
            if not run:
                raise HTTPException(404, "Run not found")
            run["actions"] = conn.execute("SELECT * FROM autotune.actions WHERE run_id=%s ORDER BY id", (run_id,)).fetchall()
            for action in run["actions"]:
                action["steps"] = conn.execute("SELECT * FROM autotune.action_steps WHERE action_id=%s ORDER BY step_number", (action["id"],)).fetchall()
            return run

    return app
