"""
routers/notifications.py – API für Scheduler-Status und manuellen Benachrichtigungs-Versand
============================================================================================

Endpunkte:
  GET  /api/notifications/jobs
       → Listet alle laufenden Scheduler-Jobs mit nächstem Ausführungszeitpunkt.

  POST /api/notifications/{router_name}/{source_id}/send_now
       → Führt den Sync-Preview sofort aus und versendet die E-Mail manuell.
         Nützlich zum Testen der SMTP-Konfiguration ohne auf den Cron zu warten.

  POST /api/notifications/reload
       → Lädt sync.json neu und registriert alle Jobs neu (z. B. nach
         Konfigurations­änderungen zur Laufzeit, ohne App-Neustart).
"""

from fastapi import APIRouter, HTTPException

from scheduler import get_scheduled_jobs, reload_scheduler
from sync_engine import sync_config, get_adapter
from notification import send_sync_notification

import sync_jira  # noqa: F401  – Adapter-Registrierung sicherstellen

router = APIRouter()


@router.get("/jobs")
def list_jobs():
    """Gibt alle laufenden Scheduler-Jobs zurück."""
    return {"jobs": get_scheduled_jobs()}


@router.post("/{router_name}/{source_id}/send_now")
def send_now(router_name: str, source_id: str):
    """
    Sofortiger Sync-Preview + E-Mail-Versand für die angegebene Quelle.
    only_if_changes wird für diesen manuellen Aufruf ignoriert –
    die E-Mail wird immer versendet (auch bei 0 Änderungen).
    """
    source_cfg = sync_config.get_source(router_name, source_id)
    if not source_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Sync-Quelle '{source_id}' für Router '{router_name}' nicht gefunden."
        )

    notif_cfg = source_cfg.get("notification", {})
    if not notif_cfg:
        raise HTTPException(
            status_code=400,
            detail="Keine Notification-Konfiguration für diese Quelle vorhanden."
        )
    if not notif_cfg.get("to"):
        raise HTTPException(
            status_code=400,
            detail="Keine Empfänger (notification.to) konfiguriert."
        )

    try:
        adapter = get_adapter(source_cfg)
        preview = adapter.fetch_preview()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fehler beim Abrufen der Jira-Daten: {exc}")

    # Für manuellen Aufruf: only_if_changes temporär deaktivieren
    notif_cfg_override = {**notif_cfg, "only_if_changes": False, "enabled": True}

    try:
        send_sync_notification(preview, notif_cfg_override)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Fehler beim E-Mail-Versand: {exc}")

    return {
        "status":  "sent",
        "to":      notif_cfg.get("to", []),
        "summary": {
            "creates": len(preview.creates),
            "updates": len(preview.updates),
            "skipped": len(preview.skipped),
        },
    }


@router.post("/reload")
def reload_jobs():
    """
    Lädt sync.json neu und registriert alle Scheduler-Jobs neu.
    Nützlich nach Änderungen an der Konfiguration zur Laufzeit.
    """
    try:
        reload_scheduler()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Fehler beim Neu-Laden: {exc}")
    return {"status": "reloaded", "jobs": get_scheduled_jobs()}
