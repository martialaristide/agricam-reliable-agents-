"""
Test de fumée du dashboard Streamlit (`dashboard/app.py`) via
`streamlit.testing.v1.AppTest` : l'application se rend sans exception sur
une base réelle produite par `scripts/run_campaign.py`, et ses widgets
répondent (sélection de campagne, comparaison, filtre de sur-confiance).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agricam_reliable_agents.persistence.engine import DATABASE_URL_ENV_VAR

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"
SCRIPT = ROOT / "scripts" / "run_campaign.py"


@pytest.fixture(scope="module")
def populated_db_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Deux campagnes simulées (p = 1.0 et p = 0.5) dans un fichier SQLite."""
    spec = importlib.util.spec_from_file_location("run_campaign_for_dashboard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    url = f"sqlite:///{(tmp_path_factory.mktemp('dash') / 'results.db').as_posix()}"
    for p_step in ("1.0", "0.5"):
        _, _, repo = module.run_campaign(module.parse_args(
            ["--n-trials", "4", "--p-step", p_step, "--database-url", url],
        ))
        repo.engine.dispose()
    return url


def _run_app(url: str, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, url)
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    return app


def test_app_renders_latest_campaign(populated_db_url: str, monkeypatch):
    app = _run_app(populated_db_url, monkeypatch)
    assert app.title[0].value.startswith("Campagne #2")  # la plus récente est sélectionnée
    labels = [m.label for m in app.metric]
    assert "p̂ global" in labels and any("Sur-confiance" in label for label in labels)
    assert any("Fiabilité par tâche" in s.value for s in app.subheader)
    assert any("pass^k" in s.value for s in app.subheader)
    assert len(app.sidebar.selectbox) == 1
    assert not any("Comparaison" in s.value for s in app.subheader)


def test_app_switches_campaign_and_compares(populated_db_url: str, monkeypatch):
    app = _run_app(populated_db_url, monkeypatch)
    app.sidebar.selectbox[0].select(1).run()
    assert not app.exception
    assert app.title[0].value.startswith("Campagne #1")
    # Campagne parfaite : 12 essais vérifiés sur 12, sur-confiance nulle.
    values = {m.label: m.value for m in app.metric}
    assert values["Essais vérifiés"] == "12 / 12"
    assert values["p̂ global"] == "100.0 %"

    app.sidebar.multiselect[0].select(2).run()
    assert not app.exception
    assert any("Comparaison" in s.value for s in app.subheader)


def test_app_overconfidence_filter(populated_db_url: str, monkeypatch):
    app = _run_app(populated_db_url, monkeypatch)
    app.toggle[0].set_value(True).run()
    assert not app.exception


def test_app_shows_hint_when_database_is_empty(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'empty.db').as_posix()}"
    monkeypatch.setenv(DATABASE_URL_ENV_VAR, url)
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    assert not app.exception
    assert any("Aucune campagne" in i.value for i in app.info)
    assert not app.main.title  # le titre de la barre latérale reste, pas celui de la page
