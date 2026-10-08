import os
from pathlib import Path
import psycopg
import pytest
import redis

import app.cache


@pytest.fixture(scope="session", autouse=True)
def setup_environment():
    if "DATABASE_URL" not in os.environ:
        os.environ["DATABASE_URL"] = "postgresql://postgres:postgres@localhost:5432/imgq"
    if "REDIS_URL" not in os.environ:
        os.environ["REDIS_URL"] = "redis://localhost:6379/0"
    app.cache._client = None


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(setup_environment):
    db_url = os.environ["DATABASE_URL"]
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))

    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            for sql_file in sql_files:
                cur.execute(sql_file.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def clean_between_tests(setup_environment):
    redis_url = os.environ["REDIS_URL"]
    r = redis.Redis.from_url(redis_url)
    r.flushall()

    # Reset postgres tables to seeded state
    db_url = os.environ["DATABASE_URL"]
    seed_file = Path(__file__).resolve().parent.parent.parent / "migrations" / "002_seed.sql"
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE jobs RESTART IDENTITY;")
            cur.execute(seed_file.read_text(encoding="utf-8"))

    yield

    r.flushall()
