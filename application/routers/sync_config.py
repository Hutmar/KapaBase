"""
routers/sync_config.py – Gibt sichere Konfigurationswerte aus sync.json zurück
(z.B. Jira-Base-URL für Link-Darstellung im Frontend)
"""
from fastapi import APIRouter
from sync_engine import sync_config

router = APIRouter()

@router.get("/jira_base_url")
def get_jira_base_url():
    """Gibt die Jira-Base-URL für die Projekt-Verlinkung zurück."""
    # Alle konfigurierten Quellen nach einer Jira-Quelle durchsuchen
    all_sources = sync_config._data.get("sync_sources", {})
    for router_name, sources in all_sources.items():
        for src in sources:
            if src.get("type") == "jira":
                base_url = src.get("jira", {}).get("base_url", "")
                # Platzhalter ignorieren
                if base_url and base_url != "JIRA_URL":
                    return {"base_url": base_url}
    return {"base_url": ""}
