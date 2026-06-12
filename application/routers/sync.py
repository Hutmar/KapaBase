"""
routers/sync.py – API-Endpunkte für die Synchronisierung
=========================================================
Generisch aufgebaut: der router-Parameter in den URLs bestimmt,
für welchen Bereich der App synchronisiert wird.

Endpunkte:
  GET  /api/sync/{router}/sources          – Liefert konfigurierte Quellen
  POST /api/sync/{router}/{source_id}/preview  – Vorschau (kein DB-Schreiben)
  POST /api/sync/{router}/{source_id}/apply    – Übernehmen bestätigter Diffs
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from sync_engine import sync_config, get_adapter, ChangeType, RecordDiff, FieldChange

# Import sorgt dafür, dass der Adapter registriert wird
import sync_jira  # noqa: F401

router = APIRouter()


# ── Pydantic-Modelle für Apply-Request ────────────────────────────────────────

class FieldChangeIn(BaseModel):
    field_name:    str
    display_name:  str
    current_value: Any = None
    new_value:     Any = None


class RecordDiffIn(BaseModel):
    change_type:    str           # "create" | "update"
    external_id:    str
    record_id:      int | None
    display_name:   str
    changes:        list[FieldChangeIn] = []
    merged_payload: dict[str, Any] = {}


class ApplyRequest(BaseModel):
    diffs: list[RecordDiffIn]


# ── Endpunkte ──────────────────────────────────────────────────────────────────

@router.get("/{router_name}/sources")
def list_sources(router_name: str):
    """Gibt alle konfigurierten Sync-Quellen für einen Router zurück."""
    sources = sync_config.get_sources_for_router(router_name)
    return {
        "router":  router_name,
        "sources": [
            {
                "id":          s.get("id"),
                "type":        s.get("type"),
                "enabled":     s.get("enabled", True),
                "description": s.get("description", ""),
            }
            for s in sources
        ],
    }


@router.post("/{router_name}/{source_id}/preview")
def preview_sync(router_name: str, source_id: str):
    """
    Holt Daten von der externen Quelle, berechnet Diffs zur DB
    und liefert eine Vorschau – ohne irgendetwas in der DB zu verändern.
    """
    source_cfg = sync_config.get_source(router_name, source_id)
    if not source_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Sync-Quelle '{source_id}' für Router '{router_name}' nicht gefunden."
        )
    if not source_cfg.get("enabled", True):
        raise HTTPException(status_code=400, detail="Diese Sync-Quelle ist deaktiviert.")

    try:
        adapter = get_adapter(source_cfg)
        preview = adapter.fetch_preview()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fehler beim Abrufen der externen Daten: {exc}")

    return preview.to_dict()


@router.post("/{router_name}/{source_id}/apply")
def apply_sync(router_name: str, source_id: str, body: ApplyRequest):
    """
    Wendet die vom Nutzer bestätigten Diffs auf die lokale DB an.
    Nur CREATE und UPDATE werden verarbeitet.
    """
    source_cfg = sync_config.get_source(router_name, source_id)
    if not source_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Sync-Quelle '{source_id}' für Router '{router_name}' nicht gefunden."
        )

    try:
        adapter = get_adapter(source_cfg)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Pydantic → interne Datenklassen konvertieren
    diffs: list[RecordDiff] = []
    for d in body.diffs:
        if d.change_type not in (ChangeType.CREATE, ChangeType.UPDATE):
            continue
        diffs.append(RecordDiff(
            change_type=ChangeType(d.change_type),
            external_id=d.external_id,
            record_id=d.record_id,
            display_name=d.display_name,
            changes=[
                FieldChange(
                    field_name=fc.field_name,
                    display_name=fc.display_name,
                    current_value=fc.current_value,
                    new_value=fc.new_value,
                )
                for fc in d.changes
            ],
            merged_payload=d.merged_payload,
        ))

    try:
        result = adapter.apply(diffs)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Fehler beim Anwenden der Sync-Daten: {exc}")

    return {
        "status":  "ok",
        "created": result["created"],
        "updated": result["updated"],
        "errors":  result["errors"],
    }
