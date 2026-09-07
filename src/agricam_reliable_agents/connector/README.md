# Connecteur d'Agent Générique

Brancher n'importe quel agent — AgriCam ou tiers, déployé ou non — sur le
harnais de fiabilité existant (`reliability/harness.py`, non modifié).
Ce document est destiné à quelqu'un qui veut connecter **son** agent : il
précise le format attendu par chaque adaptateur, pas seulement le code.

Vision d'ensemble et justification de chaque choix de conception :
`docs/` du dépôt principal, document de référence
*AgriCam_Connecteur_Generique_Vision_Fiabilite*.

## Principe

```
AgentConnector.send(prompt, trial_id) -> RawAgentReply
                    │
                    ▼ (bridge.agent_connector_as_runner)
AgentRunner(task, trial_id) -> AgentResult
                    │
                    ▼
        reliability/harness.py::evaluate_task (inchangé)
```

Un connecteur ne voit qu'un **prompt texte** et répond avec un **texte** —
jamais un appel d'outil (`AgentResult.tool_calls` reste toujours vide pour
un agent connecté : le connecteur ne sait pas ce que l'agent a fait en
interne, et n'a pas besoin de le savoir). Seul l'agent AgriCam interne
(`agent/loop.py::run_agent`) a cette visibilité, parce qu'on en contrôle
la boucle.

```python
from agricam_reliable_agents.connector.direct_model_adapter import DirectModelAdapter
from agricam_reliable_agents.connector.bridge import agent_connector_as_runner
from agricam_reliable_agents.reliability.harness import evaluate_task, HarnessConfig

connector = DirectModelAdapter(mon_llm_client, system_prompt="Tu es un agent utile.")
runner = agent_connector_as_runner(connector)
report = evaluate_task(ma_tache, runner, mon_snapshot_fn, HarnessConfig(n_trials=20))
```

## Adaptateur « appel direct modèle » (`direct_model_adapter.py`)

Le plus proche de l'agent AgriCam actuel : un prompt système fixe + un
`LLMClient` (`agent/llm_client.py`) — Claude, ou tout autre modèle qui
implémente le même contrat minimal (`generate(messages, tool_schemas) ->
LLMResponse`).

- **Entrée** : le prompt de la tâche, préfixé du prompt système s'il y en
  a un.
- **Sortie** : `LLMResponse.final_text` (chaîne vide si le modèle ne
  produit aucun texte).
- **Succès déclaré** : `SuccessClaimExtractor` fourni à la connexion
  (défaut : le classifieur par motifs d'AgriCam — à remplacer si votre
  agent ne formule pas un succès de la même façon).
- **Pannes** : une `LLMClientError` du client est capturée dans
  `RawAgentReply.error`, jamais levée.

```python
DirectModelAdapter(mon_llm_client, system_prompt="...", success_claim_extractor=mon_extracteur)
```

## Adaptateur REST/HTTP (`rest_adapter.py`)

Pour un agent exposé via une API (webhook, endpoint `/chat`...).

**Format par défaut** (personnalisable, voir plus bas) :

| | Méthode | Corps |
|---|---|---|
| Requête | `POST <endpoint>` | `{"prompt": "<texte>", "trial_id": <entier>}` |
| Réponse | `200 OK` | `{"text": "<texte>", "declared_success": <bool>}` |

Si votre API a une autre forme, fournissez :

```python
RestAdapter(
    "https://mon-agent.example/chat",
    headers={"Authorization": "Bearer ..."},
    request_builder=lambda prompt, trial_id: {"input": {"message": prompt}, "run": trial_id},
    response_parser=lambda payload: (payload["output"]["message"], payload["output"]["ok"]),
)
```

`request_builder(prompt, trial_id) -> dict` construit le corps JSON
envoyé ; `response_parser(payload) -> (texte, succès_déclaré)` extrait la
réponse du corps JSON désérialisé (`ValueError`/`TypeError` levée à
l'intérieur est capturée, jamais propagée).

**Pannes capturées** (jamais levées) : statut HTTP en erreur (4xx/5xx),
panne de transport (connexion refusée, DNS, timeout), JSON invalide ou de
forme inattendue.

**Authentification** : `headers=` au constructeur. Ne jamais coder une
clé en dur — en production, la vraie valeur vient de `credential_ref`
(voir `oracle_repository.py`), jamais stockée en clair.

## Adaptateur client MCP (`mcp_client_adapter.py`)

Pour un agent exposé comme serveur MCP (transport stdio) — le nôtre
(`mcp_tools/server.py`) ou un tiers.

**Format par défaut** : appelle l'outil `tool_name` (défaut `"chat"`)
avec `{"prompt": "<texte>"}` ; le premier bloc `TextContent` du
`CallToolResult` est la réponse.

```python
from mcp.client.stdio import StdioServerParameters
from agricam_reliable_agents.connector.mcp_client_adapter import connect_stdio

params = StdioServerParameters(command="python", args=["-m", "mon_agent_mcp"])
with connect_stdio(params, tool_name="chat") as connecteur:
    reponse = connecteur.send("Bonjour", trial_id=0)
```

Pour appeler un outil métier existant plutôt qu'un outil conversationnel
dédié, personnalisez `tool_name`/`build_arguments` :

```python
connect_stdio(params, tool_name="check_marketplace_stock",
              build_arguments=lambda prompt: {"product_id": "PRD-7"})
```

`connect_stdio` gère l'ouverture ET la fermeture du sous-processus et de
la session (`with`) ; la session reste vivante entre les essais d'une
campagne — ne PAS rouvrir une connexion par essai.

**Pannes capturées** : erreur métier de l'outil (`is_error=True` côté
MCP) ou panne de transport (session/processus fermé) — jamais levées.

## Adaptateur CLI/sous-processus (`cli_adapter.py`)

Pour un agent exécuté en local (script, binaire). **Isolation
obligatoire, sans dérogation possible** : `shell=False` systématique
(`command` est TOUJOURS une liste d'arguments, jamais une chaîne — une
chaîne unique est rejetée à la construction), et `timeout` obligatoire
(pas de valeur par défaut infinie).

- **Entrée** : le prompt sur l'entrée standard du sous-processus
  (personnalisable via `build_stdin`).
- **Sortie** : `stdout`, débarrassé des espaces de bord (personnalisable
  via `parse_output`).
- **Échec** : code de sortie non nul → `RawAgentReply.error` (avec
  `stderr` si non vide) ; timeout → sous-processus tué proprement
  (`subprocess.run` s'en charge), jamais de processus zombie.

```python
CliAdapter(["python", "mon_agent.py", "--fast"], timeout=30.0)
```

Pourquoi aucun filtrage de métacaractères shell n'est nécessaire : sous
`shell=False`, chaque élément de `command` est transmis tel quel à
`execve`/`CreateProcess`, jamais interprété par un interpréteur de
commandes — l'injection shell est structurellement impossible, pas
seulement filtrée.

## Définir un oracle : approbation de diff (mode recommandé)

Écrire `expected_state_delta` de mémoire fait courir un risque
documenté : on oublie plus facilement d'écrire un champ qu'on ne le
remarque en le voyant apparaître. Le mode recommandé inverse la charge :

```python
from agricam_reliable_agents.connector.oracle_approval import capture_baseline, approve_baseline

# 1. Une exécution de référence, jugée correcte, une seule fois.
baseline = capture_baseline(ma_tache, mon_agent_de_reference, mon_snapshot_fn)
print(baseline.diff)  # l'INTÉGRALITÉ de ce qui a changé — à relire en entier

# 2. Un humain choisit les champs à vérifier à chaque essai futur.
oracle = approve_baseline(oracle_repository, baseline, approved_fields={"diagnostic.D-42.status"})
```

`oracle.validated_by_human` est alors vrai — condition nécessaire
(vérifiée par `orchestration.require_validated_oracle`, non
contournable en Python) pour qu'une campagne utilise cette tâche.

## Environnements et mode dry-run

```python
from agricam_reliable_agents.connector.environment import agricam_runner_for_environment

runner, snapshot_fn, tools = agricam_runner_for_environment(
    llm_client, policy, "production", confirmed=True, dry_run=True,
)
```

- `environment` : `"test"` | `"staging"` | `"production"`. Une campagne
  visant `"production"` sans `confirmed=True` lève
  `ProductionConfirmationRequiredError` avant même d'ouvrir un store.
- `dry_run=True` : les outils à effet de bord (`SENSITIVE_TOOLS`,
  extensible via `sensitive_tools=`) sont exécutés puis leur effet réel
  est défait — l'agent voit un succès plausible, rien ne bouge
  réellement. Chaque interception est journalisée (`tools.log`).

## Ce que ce connecteur ne fait PAS

- Il ne devine jamais un oracle à votre place : l'inférence de contrat
  (`contract_inference.py`) ne propose qu'un point de départ, jamais une
  validation — voir la Partie 1, section 3.6 du document de référence.
- Il ne modifie ni `reliability/harness.py`, ni
  `verifier/success_verifier.py`, ni `security/guard.py` : il s'adapte à
  ces modules, jamais l'inverse.
- Il ne surveille pas un agent en production dans la durée (mode
  « shadow », détection de dérive CUSUM/EWMA) — feuille de route
  documentée en Partie 2 du document de référence, pas un livrable de ce
  paquet.
