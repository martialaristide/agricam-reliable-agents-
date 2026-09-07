"""
Sélection du store du serveur MCP par `AGRICAM_DATABASE_URL`.

En mémoire (`tools_from_env` avec un environnement explicite) puis en
sous-processus réel : le module lancé avec la variable pointant sur un
fichier SQLite doit créer la base, la semer une seule fois et y écrire.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agricam_reliable_agents.mcp_tools.data_store import AgriCamDataStore
from agricam_reliable_agents.mcp_tools.server import tools_from_env
from agricam_reliable_agents.mcp_tools.sql_store import SqlAlchemyDataStore
from agricam_reliable_agents.persistence.engine import DATABASE_URL_ENV_VAR

SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")

pytestmark = pytest.mark.anyio


def test_tools_from_env_defaults_to_in_memory_store():
    tools = tools_from_env({})
    assert isinstance(tools.store, AgriCamDataStore)
    assert tools.store.snapshot()["product.PRD-7.stock_qty"] == 25

    tools = tools_from_env({DATABASE_URL_ENV_VAR: ""})
    assert isinstance(tools.store, AgriCamDataStore)


def test_tools_from_env_uses_sql_store_and_seeds_once(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'mcp.db').as_posix()}"
    tools = tools_from_env({DATABASE_URL_ENV_VAR: url})
    assert isinstance(tools.store, SqlAlchemyDataStore)
    tools.recommend_treatment("D-42")
    tools.store.engine.dispose()

    again = tools_from_env({DATABASE_URL_ENV_VAR: url})
    assert again.store.snapshot()["diagnostic.D-42.status"] == "treated"  # pas re-semé
    assert len(again.store.get_diagnostic_history()) == 1
    again.store.engine.dispose()


def test_tools_from_env_reads_process_environment(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, "sqlite://")
    assert isinstance(tools_from_env().store, SqlAlchemyDataStore)


async def test_real_subprocess_persists_to_sqlite_file(tmp_path: Path):
    db_path = tmp_path / "server.db"
    env = {
        **os.environ,
        "PYTHONPATH": SRC_DIR,
        "PYTHONIOENCODING": "utf-8",
        DATABASE_URL_ENV_VAR: f"sqlite:///{db_path.as_posix()}",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agricam_reliable_agents.mcp_tools.server"], env=env,
    )
    with anyio.fail_after(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                treated = await session.call_tool("recommend_treatment", {"diagnostic_id": "D-42"})

    assert treated.is_error is not True
    assert db_path.exists()
    store = SqlAlchemyDataStore.from_url(f"sqlite:///{db_path.as_posix()}")
    assert store.snapshot() == {
        "diagnostic.D-42.status": "treated",
        "product.PRD-7.stock_qty": 24,
        "farmer.F-001.notified_count": 0,
    }
    store.engine.dispose()
