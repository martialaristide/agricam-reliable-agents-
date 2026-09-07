"""
Adaptateur REST/HTTP — Phase B du plan d'implémentation.

Pour un agent exposé via une API (webhook, endpoint `/chat`...). Utilise
`httpx`, déjà en dépendance du projet (via `anthropic`, et directement
listé dans `requirements.txt` pour l'interface de vérification). Testé
uniquement avec `httpx.MockTransport` : aucun appel réseau réel n'a lieu
dans la suite de tests de ce module.

Format de requête/réponse par défaut (pour un tiers qui lirait ce code)
-------------------------------------------------------------------------
Requête  : `POST <endpoint>`, corps JSON `{"prompt": <str>, "trial_id": <int>}`.
Réponse  : `200 OK`, corps JSON `{"text": <str>, "declared_success": <bool>}`.

Un agent dont l'API a une forme différente fournit `request_builder`
(`(prompt, trial_id) -> dict JSON`) et `response_parser`
(`(dict JSON désérialisé) -> (texte, succès_déclaré)`) au constructeur,
sans avoir besoin de sous-classer quoi que ce soit.

Authentification : passée via `headers` au constructeur (ex.
`{"Authorization": "Bearer sk-..."}`) — jamais codée en dur ici. En
production, la vraie valeur vient de `credential_ref`
(`connector/oracle_repository.py`, Tâche 5), jamais stockée en clair.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Self

import httpx

from agricam_reliable_agents.connector.base import ConnectorMetadata, RawAgentReply

CONNECTOR_TYPE = "rest"
DEFAULT_TIMEOUT_SECONDS = 30.0


def default_request_builder(prompt: str, trial_id: int) -> dict[str, Any]:
    """Corps JSON par défaut : `{"prompt": prompt, "trial_id": trial_id}`."""
    return {"prompt": prompt, "trial_id": trial_id}


def default_response_parser(payload: Any) -> tuple[str, bool]:
    """Lit `{"text": ..., "declared_success": ...}` dans le corps JSON
    désérialisé. Lève `TypeError` si `payload` n'est pas un objet JSON
    (ex. une liste ou un scalaire) — traité par `send()` comme une panne
    de format, jamais laissé remonter tel quel."""
    if not isinstance(payload, dict):
        raise TypeError(f"Réponse JSON inattendue (attendu un objet) : {payload!r}")
    return str(payload.get("text", "")), bool(payload.get("declared_success", False))


class RestAdapter:
    """
    `AgentConnector` par appel HTTP `POST` configurable.

    Args:
        endpoint: URL complète appelée pour chaque essai.
        headers: en-têtes envoyés à chaque requête (authentification
            incluse) ; ignoré si `client` est fourni explicitement.
        client: `httpx.Client` déjà configuré (utile pour injecter un
            `transport=httpx.MockTransport(...)` en test, ou un client
            partagé entre plusieurs connexions) ; sinon, un client dédié
            est créé et fermé par `close()`.
        request_builder: construit le corps JSON envoyé, depuis
            `(prompt, trial_id)`.
        response_parser: extrait `(texte, succès_déclaré)` du corps JSON
            de la réponse.
        timeout: délai en secondes avant `httpx.TimeoutException` (ignoré
            si `client` est fourni — le client porte alors son propre
            réglage).
        name: nom de cette connexion, pour `describe()`.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
        request_builder: Callable[[str, int], dict[str, Any]] = default_request_builder,
        response_parser: Callable[[Any], tuple[str, bool]] = default_response_parser,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        name: str = "rest-agent",
    ) -> None:
        self._endpoint = endpoint
        self._request_builder = request_builder
        self._response_parser = response_parser
        self._name = name
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(headers=headers, timeout=timeout)

    def send(self, prompt: str, trial_id: int) -> RawAgentReply:
        start = time.monotonic()
        body = self._request_builder(prompt, trial_id)
        try:
            response = self._client.post(self._endpoint, json=body)
            response.raise_for_status()
            payload = response.json()
            text, declared_success = self._response_parser(payload)
        except (ValueError, TypeError) as exc:
            # JSON invalide (response.json() -> json.JSONDecodeError, une
            # ValueError) ou forme inattendue (response_parser -> TypeError).
            return RawAgentReply(
                text="", declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=f"Réponse invalide de l'agent : {exc}",
            )
        except httpx.HTTPError as exc:
            # Couvre à la fois les pannes de transport (connexion refusée,
            # DNS, timeout) et les statuts HTTP en erreur (raise_for_status) :
            # httpx.HTTPStatusError et httpx.TransportError sont toutes deux
            # des sous-classes de httpx.HTTPError.
            return RawAgentReply(
                text="", declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000, error=str(exc),
            )

        return RawAgentReply(
            text=text, declared_success=declared_success,
            latency_ms=(time.monotonic() - start) * 1000, raw=payload,
        )

    def describe(self) -> ConnectorMetadata:
        return ConnectorMetadata(connector_type=CONNECTOR_TYPE, name=self._name, capabilities=("http",))

    def close(self) -> None:
        """Ferme le client HTTP si ce connecteur en possède un (créé par
        défaut au constructeur) — ne ferme jamais un `client` fourni par
        l'appelant, qui en reste responsable."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
