"""
AgriCam Reliable Agents — un agent qui tient dans la durée, et le harnais
qui le prouve.

Paquet organisé en sept modules : `models` (contrats de données),
`mcp_tools` (outils + serveur MCP + stores mémoire/SQL), `agent` (boucle
ReAct + client LLM), `reliability` (harnais pass^k + dépôt de campagnes),
`verifier` (Success Verifier), `security` (politique, garde, sanitisation
OWASP LLM) et `persistence` (fabrique de moteur SQLAlchemy).
"""

__version__ = "0.3.0"
