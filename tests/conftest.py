"""Configuration pytest partagée."""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Les tests asynchrones (serveur MCP) tournent uniquement sur asyncio :
    `trio` n'est pas une dépendance du projet."""
    return "asyncio"
