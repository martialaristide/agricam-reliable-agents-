"""
Connecteur d'Agent Générique — brancher n'importe quel agent (AgriCam ou
tiers, déployé ou non) sur le harnais de fiabilité existant.

Partie 1 du document de référence
`AgriCam_Connecteur_Generique_Vision_Fiabilite` (sections 1 à 7). La
Partie 2 (surveillance continue, dérive CUSUM/EWMA, passeport de
fiabilité) est une feuille de route documentée, pas un livrable de ce
paquet — les points d'extension naturels vers elle sont marqués
`# EXTENSION FUTURE (Partie 2, hors périmètre)` dans le code.

Principe de conception non négociable : ce paquet s'adapte à
`reliability/harness.py`, `verifier/success_verifier.py`,
`reliability/stats.py` et `security/guard.py` — il ne les modifie jamais.
Un connecteur produit un `AgentResult` du même contrat que l'agent AgriCam
interne (`agent/loop.py`) ; le harnais ne sait pas faire la différence.

Modules
-------
- `base` : `AgentConnector` (Protocol), `RawAgentReply`, `ConnectorMetadata`.
- `bridge` : adapte un `AgentConnector` au contrat `AgentRunner` du harnais.
- `direct_model_adapter` : connecteur pour un LLM nu (prompt + modèle).
- `rest_adapter` : connecteur HTTP/REST.
- `mcp_client_adapter` : connecteur client MCP.
- `cli_adapter` : connecteur sous-processus, isolé (`shell=False`, timeout).
- `oracle_repository` : tables `agent_connections` / `task_oracles`.
- `oracle_approval` : oracle par approbation de diff (approval testing).
- `oracle_mutation` : audit de l'oracle par mutation ciblée à la frontière
  des outils.
- `orchestration` : couche qui applique la contrainte
  `validated_by_human` avant toute campagne — jamais contournable en
  appelant directement une fonction Python.
- `contract_inference` : proposition d'oracle candidat à partir d'un
  schéma d'outil (mode assisté, jamais auto-validé).
"""
