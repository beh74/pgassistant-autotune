from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.settings import Settings


def connect(settings: Settings, *, collector=False):
    dsn = settings.collector_dsn if collector else settings.database_url.get_secret_value()
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def migrate(settings: Settings):
    with connect(settings) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(149704021)")
        conn.execute((Path(__file__).resolve().parents[1] / "sql" / "schema.sql").read_text())


if __name__ == "__main__":
    migrate(Settings())
