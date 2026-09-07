"""
Tâche 4 (Phase B, complément sécurité) — adaptateur CLI/sous-processus
avec isolation.

Le script factice ci-dessous est créé dans les fixtures de test (jamais
un binaire du système) et lancé via `sys.executable` : portable, aucune
dépendance externe, aucun accès réseau.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from agricam_reliable_agents.connector.bridge import agent_connector_as_runner
from agricam_reliable_agents.connector.cli_adapter import (
    CONNECTOR_TYPE,
    CliAdapter,
    default_build_stdin,
    default_parse_output,
)
from agricam_reliable_agents.models.data_models import Task, TaskComplexity
from agricam_reliable_agents.reliability.harness import HarnessConfig, evaluate_task

FAKE_AGENT_SCRIPT = """
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "success"
prompt = sys.stdin.read()

if mode == "success":
    print("Traitement appliqué avec succès pour : " + prompt.strip())
    sys.exit(0)
elif mode == "failure":
    print("sortie partielle")
    sys.stderr.write("erreur simulée : action impossible")
    sys.exit(1)
elif mode == "hang":
    time.sleep(2.0)
    with open(sys.argv[2], "w") as f:
        f.write("toujours vivant après le délai")
    sys.exit(0)
elif mode == "echo_json":
    import json
    data = json.loads(prompt)
    print(json.dumps({"reply": data["message"].upper()}))
    sys.exit(0)
"""


@pytest.fixture
def fake_agent(tmp_path: Path) -> Path:
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT_SCRIPT, encoding="utf-8")
    return script


def make_task() -> Task:
    return Task(
        id="T-CLI", prompt="Traite le diagnostic D-42.", complexity=TaskComplexity.LEVEL_1,
        category="generic", expected_state_delta={}, verification_query="q",
    )


# ---------------------------------------------------------------------------
# Validation de la commande et du timeout
# ---------------------------------------------------------------------------


def test_command_as_a_string_is_rejected():
    with pytest.raises(TypeError, match="chaîne"):
        CliAdapter("python agent.py", timeout=5.0)


def test_empty_command_is_rejected():
    with pytest.raises(ValueError, match="vide"):
        CliAdapter([], timeout=5.0)


def test_non_string_argument_is_rejected():
    with pytest.raises(TypeError, match="chaîne"):
        CliAdapter([sys.executable, 42], timeout=5.0)  # type: ignore[list-item]


@pytest.mark.parametrize("timeout", [0, -1, -0.5])
def test_non_positive_timeout_is_rejected(fake_agent: Path, timeout: float):
    with pytest.raises(ValueError, match="positif"):
        CliAdapter([sys.executable, str(fake_agent)], timeout=timeout)


# ---------------------------------------------------------------------------
# send() : succès, échec, forme de la commande réellement passée
# ---------------------------------------------------------------------------


def test_send_success_path(fake_agent: Path):
    adapter = CliAdapter([sys.executable, str(fake_agent), "success"], timeout=10.0)
    reply = adapter.send("Traite D-42.", trial_id=0)

    assert reply.error is None
    assert reply.text == "Traitement appliqué avec succès pour : Traite D-42."
    assert reply.declared_success is True
    assert reply.latency_ms >= 0.0
    assert reply.raw is not None


def test_send_failure_path_captures_exit_code_and_stderr():
    adapter = CliAdapter([sys.executable, "-c", "import sys; sys.exit(1)"], timeout=10.0)
    reply = adapter.send("x", trial_id=0)

    assert reply.error is not None
    assert "1" in reply.error
    assert reply.declared_success is False


def test_send_failure_path_includes_stderr_message(fake_agent: Path):
    adapter = CliAdapter([sys.executable, str(fake_agent), "failure"], timeout=10.0)
    reply = adapter.send("x", trial_id=0)

    assert reply.error is not None
    assert "erreur simulée" in reply.error
    assert reply.text == "sortie partielle"  # stdout reste lisible même en échec
    assert reply.declared_success is False


def test_never_invokes_a_shell(monkeypatch, fake_agent: Path):
    captured_kwargs = {}
    real_run = subprocess.run

    def spy_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    CliAdapter([sys.executable, str(fake_agent), "success"], timeout=10.0).send("x", 0)

    assert captured_kwargs["shell"] is False


# ---------------------------------------------------------------------------
# Timeout : le sous-processus est réellement tué, pas seulement abandonné
# ---------------------------------------------------------------------------


def test_timeout_kills_the_subprocess_no_zombie_survives(tmp_path: Path, fake_agent: Path):
    """Le script « hang » dort 2 s puis écrit un fichier sentinelle. Avec un
    timeout de 0,3 s, si le sous-processus n'était PAS réellement tué, le
    fichier apparaîtrait quand même après coup ; on attend au-delà des 2 s
    du sommeil pour vérifier son absence — preuve qu'il a bien été tué,
    pas seulement délaissé en arrière-plan."""
    sentinel = tmp_path / "sentinel.txt"
    adapter = CliAdapter([sys.executable, str(fake_agent), "hang", str(sentinel)], timeout=0.3)

    start = time.monotonic()
    reply = adapter.send("x", trial_id=0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.5  # send() est revenu vite : il n'a pas attendu les 2 s du sommeil
    assert reply.error is not None
    assert "0.3" in reply.error or "Délai dépassé" in reply.error
    assert reply.declared_success is False

    time.sleep(2.2)  # laisse largement passer le sommeil que le script AURAIT terminé
    assert not sentinel.exists()  # le processus a bien été tué, pas seulement abandonné


def test_timeout_error_message_names_the_configured_duration(fake_agent: Path):
    adapter = CliAdapter([sys.executable, str(fake_agent), "hang", str(Path("nowhere.txt"))], timeout=0.2)
    reply = adapter.send("x", trial_id=0)
    assert "0.2" in reply.error


# ---------------------------------------------------------------------------
# Pannes de lancement (exécutable introuvable)
# ---------------------------------------------------------------------------


def test_missing_executable_is_captured_not_raised():
    adapter = CliAdapter(["un-executable-qui-n-existe-pas-du-tout"], timeout=5.0)
    reply = adapter.send("x", trial_id=0)
    assert reply.error is not None
    assert reply.declared_success is False


# ---------------------------------------------------------------------------
# Mapping stdin/stdout personnalisé
# ---------------------------------------------------------------------------


def test_default_build_stdin_and_parse_output():
    assert default_build_stdin("Traite D-42.") == "Traite D-42."
    assert default_parse_output("  Fait.  \n") == "Fait."


def test_custom_build_stdin_and_parse_output(fake_agent: Path):
    import json

    adapter = CliAdapter(
        [sys.executable, str(fake_agent), "echo_json"], timeout=10.0,
        build_stdin=lambda prompt: json.dumps({"message": prompt}),
        parse_output=lambda stdout: json.loads(stdout)["reply"],
    )
    reply = adapter.send("bonjour", trial_id=0)
    assert reply.text == "BONJOUR"
    assert reply.error is None


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


def test_describe_reports_connector_type_and_name(fake_agent: Path):
    adapter = CliAdapter([sys.executable, str(fake_agent)], timeout=5.0, name="agent-local")
    meta = adapter.describe()
    assert meta.connector_type == CONNECTOR_TYPE == "cli"
    assert meta.name == "agent-local"
    assert "subprocess" in meta.capabilities


# ---------------------------------------------------------------------------
# Intégration bout en bout : CLI + harnais EXISTANT (non modifié)
# ---------------------------------------------------------------------------


def test_full_campaign_through_the_existing_harness_with_a_cli_connected_agent(fake_agent: Path):
    adapter = CliAdapter([sys.executable, str(fake_agent), "success"], timeout=10.0)
    runner = agent_connector_as_runner(adapter)

    report = evaluate_task(
        make_task(), runner, state_snapshot_fn=lambda t: {},
        config=HarnessConfig(n_trials=3, k_values=(1,)),
    )

    assert report.n_trials == 3
    assert report.n_success == 3
    assert report.p_hat == 1.0
