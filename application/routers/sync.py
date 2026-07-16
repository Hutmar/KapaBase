"""
routers/sync.py – API-Endpunkte für die Synchronisierung
=========================================================
Generisch aufgebaut: der router-Parameter in den URLs bestimmt,
für welchen Bereich der App synchronisiert wird.

Endpunkte:
  GET    /api/sync/{router}/sources                 – Liefert konfigurierte Quellen
  POST   /api/sync/{router}/{source_id}/preview      – Vorschau (kein DB-Schreiben)
  POST   /api/sync/{router}/{source_id}/apply        – Übernehmen bestätigter Diffs
  GET    /api/sync/{router}/{source_id}/ignore       – Liste dauerhaft ignorierter externer IDs
  POST   /api/sync/{router}/{source_id}/ignore       – Externe ID dauerhaft ignorieren
  DELETE /api/sync/{router}/{source_id}/ignore/{id}  – Ignorieren wieder aufheben
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from sync_engine import sync_config, get_adapter, ChangeType, RecordDiff, FieldChange, sync_ignore_store

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


class IgnoreRequest(BaseModel):
    external_id: str


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

    result = preview.to_dict()

    # Dauerhaft ignorierte Projekte/Issues markieren, damit das Frontend
    # die "Nie synchronisieren"-Checkbox vorbelegen und die
    # Übernahme-Checkbox standardmäßig deaktivieren kann.
    ignored_ids = set(sync_ignore_store.get_ignored(router_name, source_id))
    for d in result["diffs"]:
        d["ignored"] = d["external_id"] in ignored_ids

    return result


@router.post("/{router_name}/{source_id}/apply")
def apply_sync(router_name: str, source_id: str, body: ApplyRequest):
    """
    Wendet die vom Nutzer bestätigten Diffs auf die lokale DB an.
    Nur CREATE und UPDATE werden verarbeitet. Dauerhaft ignorierte
    externe IDs werden als Sicherheitsnetz zusätzlich serverseitig
    herausgefiltert.
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

    ignored_ids = set(sync_ignore_store.get_ignored(router_name, source_id))

    # Pydantic → interne Datenklassen konvertieren
    diffs: list[RecordDiff] = []
    for d in body.diffs:
        if d.change_type not in (ChangeType.CREATE, ChangeType.UPDATE):
            continue
        if d.external_id in ignored_ids:
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


# ── Ignorierte Projekte/Issues (dauerhaft von der Synchronisierung ausschließen) ──

@router.get("/{router_name}/{source_id}/ignore")
def list_ignored(router_name: str, source_id: str):
    """Gibt alle für diese Quelle dauerhaft ignorierten externen IDs zurück."""
    return {"ignored": sync_ignore_store.get_ignored(router_name, source_id)}


@router.post("/{router_name}/{source_id}/ignore")
def ignore_item(router_name: str, source_id: str, body: IgnoreRequest):
    """Markiert ein Projekt/Issue so, dass es nie mehr synchronisiert wird."""
    sync_ignore_store.add(router_name, source_id, body.external_id)
    return {"status": "ok", "external_id": body.external_id, "ignored": True}


@router.delete("/{router_name}/{source_id}/ignore/{external_id}")
def unignore_item(router_name: str, source_id: str, external_id: str):
    """Entfernt ein Projekt/Issue wieder von der Ignorier-Liste."""
    sync_ignore_store.remove(router_name, source_id, external_id)
    return {"status": "ok", "external_id": external_id, "ignored": False}