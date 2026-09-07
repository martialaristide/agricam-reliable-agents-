"""
Fabrique de moteur SQLAlchemy, unique point de lecture de
`AGRICAM_DATABASE_URL`.

Décisions de conception
-----------------------
- SQLite en mémoire (`sqlite://`, `sqlite:///:memory:`, ou toute URL avec
  `:memory:` / `mode=memory`) est monté sur un `StaticPool` : sans cela,
  chaque connexion du pool verrait une base vide différente, et deux
  sessions successives ne partageraient rien. Une URL SQLite fichier
  (`sqlite:////tmp/x.db`) n'entre dans AUCUN de ces cas et garde le pool
  par défaut de SQLAlchemy (`QueuePool`) — voir `is_memory_sqlite_url`,
  seule fonction qui décide, pour ne jamais laisser un préfixe `sqlite`
  générique déclencher `StaticPool` par accident.
- `check_same_thread=False` pour SQLite : Streamlit et le serveur MCP
  peuvent solliciter le moteur depuis un autre thread que celui qui l'a
  créé. Attention : un `StaticPool` partage UNE connexion entre tous les
  threads ; la bibliothèque `sqlite3` sérialise chaque appel (threadsafety
  3) mais pas les transactions. Une base en mémoire n'est donc pas faite
  pour être écrite depuis plusieurs threads : l'API refuse d'y lancer des
  campagnes en arrière-plan.
- `PRAGMA foreign_keys=ON` à chaque connexion SQLite : SQLite n'applique
  PAS les clés étrangères par défaut (contrairement à PostgreSQL), ce qui
  rendait `ForeignKey("campaigns.id")` sur `trials`/`security_incidents`
  purement décoratif jusqu'ici — un essai pouvait référencer une campagne
  inexistante sans erreur. L'événement `connect` ci-dessous corrige cela
  pour toute nouvelle connexion, y compris celles créées par le pool.
- Aucune dépendance à un pilote PostgreSQL ici : l'URL décide
  (`postgresql+psycopg://...`), le pilote doit simplement être installé ;
  PostgreSQL applique les clés étrangères nativement, sans configuration.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

DATABASE_URL_ENV_VAR = "AGRICAM_DATABASE_URL"
DEFAULT_DATABASE_URL = "sqlite:///agricam.db"

_SQLITE_MEMORY_URLS = frozenset({"sqlite://", "sqlite:///:memory:"})


def is_memory_sqlite_url(url: str) -> bool:
    """Vrai pour toute URL SQLite en mémoire (formes courtes, pilote explicite, mode=memory)."""
    if not url.startswith("sqlite"):
        return False
    return url in _SQLITE_MEMORY_URLS or ":memory:" in url or "mode=memory" in url or url.endswith("://")


def database_url_from_env(environ: Mapping[str, str] | None = None) -> str:
    """Retourne `AGRICAM_DATABASE_URL`, ou `DEFAULT_DATABASE_URL` si absente ou vide."""
    env = os.environ if environ is None else environ
    return env.get(DATABASE_URL_ENV_VAR) or DEFAULT_DATABASE_URL


def create_engine_from_url(url: str, **kwargs: Any) -> Engine:
    """Construit un moteur adapté à l'URL (options SQLite ajoutées si besoin)."""
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        connect_args = dict(kwargs.pop("connect_args", {}))
        connect_args.setdefault("check_same_thread", False)
        kwargs["connect_args"] = connect_args
        if is_memory_sqlite_url(url):
            kwargs.setdefault("poolclass", StaticPool)
    engine = create_engine(url, **kwargs)
    if is_sqlite:
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    """Active l'application des clés étrangères sur une connexion SQLite brute.

    Enregistré via `event.listen(engine, "connect", ...)` plutôt que sur la
    classe `Engine` globale : ne doit s'appliquer qu'aux moteurs SQLite créés
    ici, jamais à un moteur PostgreSQL qui n'en a pas besoin.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
