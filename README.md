# pgAssistant Auto-Tune

**Turn PostgreSQL recommendations into controlled actions across your database fleet.**

Auto-Tune reads recommendations collected from pgAssistant and applies the ones you
choose. Use one YAML policy across your fleet, filter by **team**, **advisor** and
**action type**, and preview the proposed SQL with **dry run** before executing it.
You can also target a single database or give specific databases their own policy.

## How it fits together

```mermaid
flowchart LR
    Fleet[(PostgreSQL fleet)] -->|Statistics| PGA[pgAssistant]
    PGA -->|Recommendations| Collector[pgAssistant Collector]
    Collector -->|Snapshots| History[(Collector database)]
    History -->|Fleet dashboards| Grafana[Grafana]
    History -->|Recommendations| Tune[Auto-Tune API + workers]
    Policy[policy.yaml] -->|Profiles and filters| Tune
    Tune -->|Authorized SQL| Fleet
```

pgAssistant advises, the collector records snapshots, Grafana makes the fleet
visible, and Auto-Tune executes the selected actions. Auto-Tune keeps its queue
and execution history in a separate PostgreSQL database, included in Docker Compose.

## Quick start

You need Docker Compose and a running pgAssistant Collector collecting the
`executive_plan` job for your databases.

**1. Configure connections.**

```sh
cp .env.example .env
```

In `.env`, set `POSTGRES_PASSWORD`, `AUTOTUNE_API_KEY` and
`AUTOTUNE_COLLECTOR_DATABASE_URL`. The collector connection needs read access to
its recommendation snapshots. Keep `AUTOTUNE_POLICY_FILE=./config/policy.yaml`
to use the default policy.

List your databases in [`config/sources.yaml`](config/sources.yaml), using the
**same IDs as the collector**:

```yaml
sources:
  - id: production-app
    enabled: true
    db_uri: postgresql://autotune:${PRODUCTION_DB_PASSWORD}@postgres-prod:5432/app
  - id: reporting
    enabled: true
    db_uri: postgresql://autotune:${REPORTING_DB_PASSWORD}@postgres-reporting:5432/reports
```

Add the password variables to `.env`. These are execution accounts: grant the
permissions required for the actions you enable. Collector-format source files
are accepted; `autotune_uri` can supply a separate execution account, taking
precedence over `db_uri` and its alias `conn_str`.

**2. Choose your profiles.**

Edit [`config/policy.yaml`](config/policy.yaml). For example, enable maintenance
for OPS recommendations while leaving configuration changes out:

```yaml
version: 1
mode: dry_run

profiles:
  global_advisor:
    enabled: true
    teams: [OPS]
    exclude_actions: [CREATE_INDEX, DROP_INDEX]
  autovacuum:
    enabled: true
    teams: [OPS]
    exclude_actions: [CONFIG_CHANGE]

limits:
  max_actions: 10
  statement_timeout_seconds: 300
  lock_timeout_seconds: 5
```

The supplied file includes all four profiles with comments. All are initially
disabled; activate only the profiles you want.

**3. Start and validate.**

```sh
docker compose up -d --build
docker compose exec worker python -m app.policies /config/policy.yaml
```

The validation command lists the effective rules without connecting to target
databases. The API is available at [localhost:8088/docs](http://localhost:8088/docs).
Starting the services does not trigger a tuning run.

## Preview, then execute

With `mode: dry_run`, submit a run for **every enabled database**:

```sh
export AUTOTUNE_API_KEY='your-api-key' # Same value as in .env

curl -X POST http://localhost:8088/api/v1/autotune-all \
  -H "X-API-Key: $AUTOTUNE_API_KEY"
```

The response returns one `run_id` per database. Inspect a run using its ID:

```sh
curl http://localhost:8088/api/v1/runs/42 \
  -H "X-API-Key: $AUTOTUNE_API_KEY"
```

You will see the proposed SQL, individual steps and action statuses.
**Dry run records the plan without connecting to target databases or executing SQL.**

When ready, change `mode` to `execute` in the policy and submit another run.
The policy is read at each submission; queued runs retain their original rules.
Each submission selects its own snapshot, so use `selection.as_of` if you need
both runs to use the same snapshot.

To tune **just one database**, use its collector ID:

```sh
curl -X POST 'http://localhost:8088/api/v1/autotune-targetid?target_id=production-app' \
  -H "X-API-Key: $AUTOTUNE_API_KEY"
```

## Choose what runs

Enable one or more advisor profiles:

| Profile | Currently executable actions |
| --- | --- |
| `global_advisor` | `CREATE_INDEX`, `DROP_INDEX`, `ANALYZE`, `VACUUM` |
| `index_advisor` | `CREATE_INDEX` only |
| `autovacuum` | `ANALYZE`, `VACUUM`, `CONFIG_CHANGE` without a restart |
| `parameter_advisor` | No executor available yet |

Each profile accepts these filters:

| Setting | Example | Effect |
| --- | --- | --- |
| `teams` | `[OPS, DEV/OPS]` | Keep recommendations assigned to those teams |
| `exclude_actions` | `[DROP_INDEX]` | Exclude a recommendation type |
| `exclude_advisors` | `[unused_non_constraint_indexes]` | Exclude a specific finding |
| `schemas` / `tables` | `[public]` / `[orders]` | Restrict the objects considered |
| `min_confidence` | `80` | Require a minimum recommendation confidence |

Teams are `DEV`, `OPS` and `DEV/OPS` (`DEV_OPS` is also accepted). `DEV/OPS` is the
shared team, not an abbreviation for selecting both DEV and OPS. Empty or omitted
filter lists impose no restriction. Team assignments come from the collector's
Executive Plan, which may classify index recommendations as `DEV_OPS`.

To exclude **SQL commands across all profiles**, add:

```yaml
restrictions:
  exclude_commands: [ALTER_TABLE, REINDEX]
```

Available commands: `CREATE_INDEX`, `DROP_INDEX`, `REINDEX`, `ANALYZE`, `VACUUM`,
`ALTER_TABLE`, `ALTER_SYSTEM`, `RELOAD_CONFIGURATION`.

**An excluded command skips the entire recommendation.** This matters because an
autovacuum maintenance script may include `ALTER TABLE`, and a Global Advisor
`DROP_INDEX` recommendation may actually propose `REINDEX`. `VACUUM (ANALYZE)` is
checked against both command exclusions.

Only reviewed, supported recommendations are included in profiles. Manual advice
and general `ALTER_TABLE` recommendations are not automatically enabled.

For a different policy on a specific database, use `target_policies`; a commented
example is included in [policy.yaml](config/policy.yaml). Overrides replace that
target's profiles, restrictions and limits. The common mode and snapshot selection
still apply.

## Monitor your fleet

```sh
# Follow worker activity, action steps, durations and errors
docker compose logs -f worker

# List recent runs for a database
curl 'http://localhost:8088/api/v1/runs?target_id=production-app' \
  -H "X-API-Key: $AUTOTUNE_API_KEY"
```

Run details retain the policy, snapshot, proposed and attempted SQL, step results
and durations. Logs include `run_id`, `target_id` and `action_id`, without connection
credentials. Grafana displays the collector's fleet recommendations and history;
Auto-Tune execution details are available through its API.

| Run status | Meaning |
| --- | --- |
| `completed` | Processing finished; inspect actions for planned, executed or skipped results |
| `no_matches` | No recommendations matched the enabled rules |
| `no_snapshot` | No snapshot within the configured time window |
| `needs_review` | Incomplete snapshot, rejected/unsupported action, failure or uncertain result |

By default, Auto-Tune uses the latest snapshot available at submission, up to
24 hours old. Adjust `selection.max_snapshot_age_seconds`, or use
`selection.lookback_seconds` / `selection.as_of` in the YAML to select older plans.
Partial snapshots are refused, with the affected advisor identified in the logs.

## Execution behavior

- Workers execute directly against target databases and coordinate per target.
  Multiple workers can serve different parts of the fleet; each needs access to
  its targets, the collector database and the Auto-Tune database.
- SQL is validated before execution. Index creation and removal use concurrent
  operations; autovacuum configuration uses `ALTER SYSTEM` followed by a reload.
  Configuration changes affect the whole PostgreSQL instance. Automatic restarts
  are not performed.
- Actions are tracked to prevent duplicate execution. Table maintenance can run
  again on a newer snapshot. Failed or uncertain attempts require review before
  retrying; multi-step scripts have no automatic rollback.

## Reference

- [Commented policy](config/policy.yaml) — profiles, filters, limits and target overrides.
- [OPS preset](config/policy_ops.yaml) — maintenance plus two autovacuum parameters;
  this preset uses **execute mode**.
- [Environment template](.env.example) — database connections, API key and policy path.
- [API documentation](http://localhost:8088/docs) — endpoints and advanced request bodies.

To select another policy, set `AUTOTUNE_POLICY_FILE` in `.env`. After changing its
path or updating the code, rebuild and recreate the services:

```sh
docker compose up -d --build api worker
```

For local development:

```sh
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[test]'
.venv/bin/pytest -q
```

Integration tests require `AUTOTUNE_TEST_DSN` pointing to a **disposable PostgreSQL
database**: they recreate test tables and the `autotune` schema.
