"""
Adaptateur « appel direct modèle » — Phase A du plan d'implémentation.

Le cas le plus proche de l'agent AgriCam actuel (section 2.2 du document
de référence) : un prompt système fixe, fourni à la connexion, plus un
`LLMClient` déjà existant (`agent/llm_client.py`, réutilisé sans
modification — aussi bien `AnthropicLLMClient` qu'un client factice de
test). Un seul aller-retour prompt -> réponse, SANS boucle d'outils : pour
un agent qui a besoin d'outils, utiliser directement `agent/loop.py`
(l'agent AgriCam) ou l'un des autres adaptateurs (REST, MCP, CLI), dont
la boucle d'outils vit côté agent tiers, pas ici.

Format de requête/réponse (pour un tiers qui lirait ce code)
-------------------------------------------------------------
Entrée : `send(prompt, trial_id)` — `prompt` est le texte de la tâche
(`Task.prompt`), tel quel, sans enrobage.
Sortie : `RawAgentReply(text=..., declared_success=..., latency_ms=...,
cost_usd=..., raw=<LLMResponse normalisée>)`. `text` est
`LLMResponse.final_text` (chaîne vide si le modèle n'a produit aucun
texte) ; `declared_success` vient du `SuccessClaimExtractor` fourni à la
connexion, appliqué à `text`.
"""

from __future__ import annotations

import time

from agricam_reliable_agents.agent.llm_client import LLMClient, LLMClientError
from agricam_reliable_agents.agent.loop import (
    DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
    SuccessClaimExtractor,
    pricing_for_model,
)
from agricam_reliable_agents.connector.base import ConnectorMetadata, RawAgentReply

CONNECTOR_TYPE = "direct_model"


class DirectModelAdapter:
    """
    `AgentConnector` qui encapsule un `LLMClient` existant et un prompt
    système fixe.

    Args:
        llm_client: n'importe quelle implémentation de `LLMClient`
            (`AnthropicLLMClient` en production, un client factice en test).
        system_prompt: prompt système fourni à CETTE connexion (section 2.2 :
            « un prompt système + un modèle »). Transmis à `llm_client.generate`
            si le client le supporte (voir note ci-dessous) ; sinon ignoré —
            un `LLMClient` minimal n'a que `generate(messages, tool_schemas)`.
        success_claim_extractor: décide si le texte produit prétend réussir ;
            par défaut le classifieur AgriCam (`PatternSuccessClaimExtractor`),
            à remplacer pour un agent qui formule un succès autrement.
        pricing: tarif (entrée, sortie) en USD/million de jetons ; par défaut
            déduit de `llm_client.model` via `pricing_for_model`.
        name: nom de cette connexion, pour `describe()`.

    Note sur `system_prompt` : le contrat minimal `LLMClient.generate` ne
    prend pas de paramètre système séparé (il opère sur une liste de
    messages) — ce connecteur préfixe donc le prompt système au premier
    message utilisateur plutôt que de supposer une méthode `system=`
    propre à `AnthropicLLMClient`. Un `AnthropicLLMClient` construit avec
    son propre `system_prompt=` (paramètre de son constructeur) reste la
    manière recommandée de fixer un vrai prompt système API ; celui-ci
    n'est qu'un filet pour tout `LLMClient` qui n'expose pas ce réglage.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        system_prompt: str | None = None,
        success_claim_extractor: SuccessClaimExtractor = DEFAULT_SUCCESS_CLAIM_EXTRACTOR,
        pricing: tuple[float, float] | None = None,
        name: str = "direct-model",
    ) -> None:
        self._llm_client = llm_client
        self._system_prompt = system_prompt
        self._success_claim_extractor = success_claim_extractor
        self._pricing = pricing or pricing_for_model(getattr(llm_client, "model", None))
        self._name = name

    def send(self, prompt: str, trial_id: int) -> RawAgentReply:
        start = time.monotonic()
        content = f"{self._system_prompt}\n\n{prompt}" if self._system_prompt else prompt
        messages = [{"role": "user", "content": content}]

        try:
            response = self._llm_client.generate(messages, [])
        except LLMClientError as exc:
            return RawAgentReply(
                text="", declared_success=False,
                latency_ms=(time.monotonic() - start) * 1000,
                error=str(exc),
            )

        text = response.final_text or ""
        cost = (
            response.input_tokens / 1_000_000 * self._pricing[0]
            + response.output_tokens / 1_000_000 * self._pricing[1]
        )
        return RawAgentReply(
            text=text,
            declared_success=self._success_claim_extractor(text),
            latency_ms=(time.monotonic() - start) * 1000,
            cost_usd=cost,
            raw=response,
        )

    def describe(self) -> ConnectorMetadata:
        return ConnectorMetadata(
            connector_type=CONNECTOR_TYPE,
            name=self._name,
            version=getattr(self._llm_client, "model", None),
            capabilities=("text",),
        )
