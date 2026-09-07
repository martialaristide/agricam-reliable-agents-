"""
Infrastructure de persistance partagée (SQLAlchemy 2.x).

Ce paquet ne contient aucun modèle : chaque domaine déclare ses propres
tables (`mcp_tools/sql_store.py` pour les données AgriCam,
`reliability/repository.py` pour les résultats de campagne). Il fournit
uniquement la fabrique de moteur commune, configurée à partir de
`AGRICAM_DATABASE_URL`.
"""

from agricam_reliable_agents.persistence.engine import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_DATABASE_URL,
    create_engine_from_url,
    database_url_from_env,
)

__all__ = [
    "DATABASE_URL_ENV_VAR",
    "DEFAULT_DATABASE_URL",
    "create_engine_from_url",
    "database_url_from_env",
]
