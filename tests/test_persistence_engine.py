from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from agricam_reliable_agents.persistence import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_DATABASE_URL,
    create_engine_from_url,
    database_url_from_env,
)


def test_database_url_from_env_falls_back_to_default():
    assert database_url_from_env({}) == DEFAULT_DATABASE_URL
    assert database_url_from_env({DATABASE_URL_ENV_VAR: ""}) == DEFAULT_DATABASE_URL
    assert database_url_from_env({DATABASE_URL_ENV_VAR: "sqlite://"}) == "sqlite://"


def test_database_url_from_env_reads_process_environment(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, "sqlite:///env.db")
    assert database_url_from_env() == "sqlite:///env.db"


def test_in_memory_sqlite_shares_state_between_connections():
    """Sans StaticPool, chaque connexion verrait une base vide distincte."""
    engine = create_engine_from_url("sqlite://")
    assert isinstance(engine.pool, StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM t")).scalar() == 1


def test_file_sqlite_does_not_use_static_pool(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'f.db').as_posix()}")
    assert not isinstance(engine.pool, StaticPool)
    engine.dispose()
