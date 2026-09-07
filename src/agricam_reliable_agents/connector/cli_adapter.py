"""
Adaptateur sous-processus/CLI — Phase B (complément sécurité) du plan
d'implémentation.

Pour un agent exécuté en local (script, binaire) : un tiers peut soumettre
du code non fiable, d'où les garanties d'isolation ci-dessous, imposées
sans dérogation possible :

- `shell=False` systématique : `command` est une LISTE d'arguments passée
  telle quelle à `execve`/`CreateProcess`, jamais une chaîne interprétée
  par un shell. C'est ce qui rend l'injection shell structurellement
  impossible ici, pas une liste de motifs interdits — un argument
  contenant `; rm -rf /` ou `` `whoami` `` est transmis à l'agent comme
  une chaîne littérale, jamais exécuté comme une commande. Filtrer les
  métacaractères shell n'aurait de sens que pour un mode `shell=True`,
  qui n'existe pas dans ce module et n'est pas prévu de l'ajouter — c'est
  pourquoi la validation ci-dessous porte sur la FORME de `command`
  (liste non vide de chaînes), pas sur son contenu.
- `timeout` obligatoire, sans valeur par défaut infinie : un agent tiers
  qui boucle ou attend indéfiniment ne doit jamais bloquer une campagne.
- stdout et stderr capturés séparément (`capture_output=True`) : le texte
  de l'agent (stdout) n'est jamais mélangé à ses diagnostics (stderr).

Format d'échange (pour un tiers qui lirait ce code)
------------------------------------------------------
Entrée : le prompt est écrit sur l'entrée standard du sous-processus
(personnalisable via `build_stdin`, ex. pour l'envelopper en JSON).
Sortie : le texte de l'agent est `stdout` (personnalisable via
`parse_output`) ; un code de sortie non nul est traité comme un échec
(`RawAgentReply.error`), quel que soit le contenu de stdout.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agricam_reliable_agents.agent.loop import (
    DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    SuccessClaimExtractor,
)
from agricam_reliable_agents.connector.base import ConnectorMetadata, RawAgentReply

CONNECTOR_TYPE = "cli"


def default_build_stdin(prompt: str) -> str:
    """Envoie le prompt tel quel sur l'entrée standard."""
    return prompt


def default_parse_output(stdout: str) -> str:
    """Le texte de l'agent est `stdout`, débarrassé des espaces de bord."""
    return stdout.strip()


def _validate_command(command: Sequence[str]) -> list[str]:
    """
    Valide la FORME de `command` (jamais son contenu, cf. docstring de
    module : `shell=False` rend le contenu inoffensif par construction).

    Refuse explicitement une chaîne unique : `subprocess.run("agent -x",
    shell=False)` traiterait la chaîne entière comme le nom d'un seul
    exécutable introuvable — une erreur silencieuse et déroutante que
    cette validation transforme en `TypeError` explicite au moment de la
    construction du connecteur, pas au premier essai d'une campagne.
    """
    if isinstance(command, (str, bytes)):
        raise TypeError(
            "command doit être une liste d'arguments, jamais une chaîne "
            "(shell=False est la seule option supportée par ce connecteur : "
            "pas d'interprétation shell, donc pas de découpage de chaîne)."
        )
    command = list(command)
    if not command:
        raise ValueError("command ne peut pas être vide.")
    for argument in command:
        if not isinstance(argument, str):
            raise TypeError(f"Chaque argument de command doit être une chaîne, reçu : {argument!r}.")
    return command


class CliAdapter:
    """
    `AgentConnector` qui exécute un agent tiers comme sous-processus,
    isolé (voir docstring de module pour les garanties).

    Args:
        command: liste d'arguments (ex. `["python", "agent.py", "--fast"]`) ;
            jamais une chaîne shell (rejeté à la construction, cf.
            `_validate_command`).
        timeout: délai en secondes avant que le sous-processus soit tué ;
            obligatoire, sans défaut — un agent tiers non fiable ne doit
            jamais pouvoir bloquer une campagne indéfiniment.
        cwd: répertoire de travail du sous-processus.
        env: variables d'environnement du sous-processus (remplace
            entièrement l'environnement hérité si fourni ; `None`
            hérite de l'environnement du processus courant, comportement
            par défaut de `subprocess.run`).
        build_stdin: construit ce qui est écrit sur l'entrée standard,
            depuis le prompt.
        parse_output: extrait le texte de l'agent depuis `stdout`.
        success_claim_extractor: décide si ce texte prétend réussir.
        name: nom de cette connexion, pour `describe()`.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout: float,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        build_stdin: Callable[[str], str] = default_build_stdin,
        parse_output: Callable[[str], str] = default_parse_output,
        success_claim_extractor: SuccessClaimExtractor = DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
        name: str = "cli-agent",
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout doit être strictement positif (pas de valeur infinie autorisée).")
        self._command = _validate_command(command)
        self._timeout = timeout
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._build_stdin = build_stdin
        self._parse_output = parse_output
        self._success_claim_extractor = success_claim_extractor
        self._name = name

    def send(self, prompt: str, trial_id: int) -> RawAgentReply:
        start = time.monotonic()
        stdin_data = self._build_stdin(prompt)
        try:
            completed = subprocess.run(
                self._command,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                shell=False,
                check=False,  # le code de sortie est interprété nous-mêmes ci-dessous
                cwd=self._cwd,
                env=self._env,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run() tue déjà le sous-processus avant de lever
            # cette exception (Popen.kill() + wait() internes) : pas de
            # processus zombie à nettoyer côté appelant.
            return RawAgentReply(
                text="", declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=f"Délai dépassé après {self._timeout} s : sous-processus tué.",
            )
        except OSError as exc:
            # Exécutable introuvable, permission refusée, etc.
            return RawAgentReply(
                text="", declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000, error=str(exc),
            )

        text = self._parse_output(completed.stdout)
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            return RawAgentReply(
                text=text, declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=f"Code de sortie {completed.returncode}" + (f" : {stderr}" if stderr else ""),
                raw=completed,
            )
        return RawAgentReply(
            text=text, declared_success=self._success_claim_extractor(text),
            latency_ms=(time.monotonic() - start) * 1000, raw=completed,
        )

    def describe(self) -> ConnectorMetadata:
        return ConnectorMetadata(connector_type=CONNECTOR_TYPE, name=self._name, capabilities=("subprocess",))
