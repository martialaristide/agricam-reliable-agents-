# AgriCam Reliable Agents — Code source vérifié

Implémentation de référence du cœur technique du projet **AgriCam Reliable
Agents** : modèles de données, outils MCP simulés, boucle agent ReAct,
harnais de fiabilité (pass^k), Success Verifier et couche de sécurité.

## État de vérification

Tout le code de ce dépôt a été **réellement exécuté** (pas seulement
rédigé) au moment de la livraison :

- ✅ 39 tests unitaires et d'intégration, tous passants
- ✅ 93 % de couverture de code (`pytest-cov`)
- ✅ 0 avertissement `ruff` (lint complet)
- ✅ Script de démonstration bout-en-bout exécuté avec succès

Seule `AnthropicLLMClient` (agent/llm_client.py) n'est pas couverte par
les tests automatisés : son exécution réelle nécessite une clé
`ANTHROPIC_API_KEY` valide. Sa structure suit le format officiel du SDK
`anthropic` (Messages API avec tool use) au moment de la rédaction ; le
reste de la boucle agent (`agent/loop.py`) est testé indépendamment via
un `LLMClient` factice (`ScriptedLLMClient`, voir `tests/test_agent_loop.py`),
ce qui permet de valider toute la logique métier sans dépendre du réseau.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Lancer les tests

```bash
PYTHONPATH=src pytest tests/ --cov=agricam_reliable_agents --cov-report=term-missing
```

## Lancer la démonstration (sans clé API)

```bash
PYTHONPATH=src python3 scripts/demo_full_pipeline.py
```

Ce script fait tourner un agent simulé avec un taux de succès réel
paramétrable (0.95 / 0.90 / 0.80) à travers le harnais de fiabilité
complet, et affiche p̂, l'intervalle de Wilson et pass^k(1, 3, 5, 10) —
démontrant numériquement l'effondrement de la fiabilité avec la longueur
de la tâche (cf. document de spécification théorique).

## Structure

```
src/agricam_reliable_agents/
├── models/data_models.py       # Contrats de données (dataclasses)
├── mcp_tools/                  # Outils MCP + store de données simulé
├── agent/                      # Boucle ReAct + interface LLM découplée
├── reliability/                # Harnais pass^k + intervalle de Wilson
├── verifier/                   # Success Verifier (diff d'état)
└── security/                   # Policy, guard, sanitizer (OWASP LLM)
tests/                          # 39 tests, 93% de couverture
scripts/demo_full_pipeline.py   # Démonstration exécutable sans API
```

## Licence

MIT.
