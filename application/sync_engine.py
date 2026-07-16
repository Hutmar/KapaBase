"""
sync_engine.py – Zentrale Synchronisierungs-Komponente
======================================================
Liest sync.json, stellt abstrakte Basis-Klassen bereit und orchestriert
die Synchronisierung zwischen externen Quellen und der lokalen Datenbank.

Erweiterbar für künftige Quellen (z.B. Azure DevOps, GitHub Projects, …).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Pfad zur Konfigurationsdatei ───────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "sync.json"

# ── Pfad zur Ignore-Liste (dauerhaft ausgeschlossene Projekte/Issues) ──────────
IGNORE_PATH = Path(__file__).parent / "sync_ignore.json"


# ── Datenklassen ───────────────────────────────────────────────────────────────

class ChangeType(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    SKIP   = "skip"


@dataclass
class FieldChange:
    """Beschreibt eine einzelne Feldänderung."""
    field_name:    str
    display_name:  str
    current_value: Any
    new_value:     Any


@dataclass
class RecordDiff:
    """Fasst alle Änderungen für einen Datensatz zusammen."""
    change_type:    ChangeType
    external_id:    str
    record_id:      Optional[int]        # None bei CREATE
    display_name:   str
    changes:        list[FieldChange] = field(default_factory=list)
    raw_external:   dict               = field(default_factory=dict)
    merged_payload: dict               = field(default_factory=dict)


@dataclass
class SyncPreview:
    """Gesamtvorschau einer Synchronisierung, wird ans Frontend geliefert."""
    source_id:   str
    source_type: str
    diffs:       list[RecordDiff]

    @property
    def creates(self) -> list[RecordDiff]:
        return [d for d in self.diffs if d.change_type == ChangeType.CREATE]

    @property
    def updates(self) -> list[RecordDiff]:
        return [d for d in self.diffs if d.change_type == ChangeType.UPDATE]

    @property
    def skipped(self) -> list[RecordDiff]:
        return [d for d in self.diffs if d.change_type == ChangeType.SKIP]

    def to_dict(self) -> dict:
        return {
            "source_id":   self.source_id,
            "source_type": self.source_type,
            "summary": {
                "creates": len(self.creates),
                "updates": len(self.updates),
                "skipped": len(self.skipped),
            },
            "diffs": [
                {
                    "change_type":  d.change_type,
                    "external_id":  d.external_id,
                    "record_id":    d.record_id,
                    "display_name": d.display_name,
                    "changes": [
                        {
                            "field_name":    fc.field_name,
                            "display_name":  fc.display_name,
                            "current_value": str(fc.current_value) if fc.current_value is not None else None,
                            "new_value":     str(fc.new_value)     if fc.new_value     is not None else None,
                        }
                        for fc in d.changes
                    ],
                    "merged_payload": d.merged_payload,
                }
                for d in self.diffs
                if d.change_type != ChangeType.SKIP
            ],
        }


# ── Konfigurationsverwaltung ───────────────────────────────────────────────────

class SyncConfig:
    """Liest und hält die Konfiguration aus sync.json."""

    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("sync.json nicht gefunden: %s", self._path)
            self._data = {"version": "1.0", "sync_sources": {}}
            return
        with open(self._path, encoding="utf-8") as fh:
            self._data = json.load(fh)
        logger.info("sync.json geladen (%s)", self._path)

    def reload(self) -> None:
        """Konfiguration neu laden (z.B. nach Dateiänderung)."""
        self._load()

    def get_sources_for_router(self, router: str) -> list[dict]:
        """Gibt alle konfigurierten Sync-Quellen für einen Router zurück."""
        return self._data.get("sync_sources", {}).get(router, [])

    def get_source(self, router: str, source_id: str) -> Optional[dict]:
        for src in self.get_sources_for_router(router):
            if src.get("id") == source_id:
                return src
        return None


# ── Ignore-Liste: Projekte/Issues, die nie mehr synchronisiert werden sollen ───

class SyncIgnoreStore:
    """
    Persistiert externe IDs (z.B. Jira-Keys), die dauerhaft von der
    Synchronisierung ausgeschlossen werden sollen – gruppiert nach
    Router und Sync-Quelle. Ablage als einfache JSON-Datei, analog zu
    sync.json, damit keine DB-Migration nötig ist.

    Struktur:
    {
        "projects": {
            "jira_projects": ["PROJ-123", "PROJ-456"]
        }
    }
    """

    def __init__(self, path: Path = IGNORE_PATH):
        self._path = path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                self._data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Konnte sync_ignore.json nicht lesen (%s) – starte mit leerer Liste.", exc)
            self._data = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error("Konnte sync_ignore.json nicht schreiben: %s", exc)

    def get_ignored(self, router: str, source_id: str) -> list[str]:
        return list(self._data.get(router, {}).get(source_id, []))

    def is_ignored(self, router: str, source_id: str, external_id: str) -> bool:
        return external_id in self._data.get(router, {}).get(source_id, [])

    def add(self, router: str, source_id: str, external_id: str) -> None:
        self._data.setdefault(router, {}).setdefault(source_id, [])
        if external_id not in self._data[router][source_id]:
            self._data[router][source_id].append(external_id)
            self._save()
            logger.info("Sync-Ignore hinzugefügt: %s/%s -> %s", router, source_id, external_id)

    def remove(self, router: str, source_id: str, external_id: str) -> None:
        lst = self._data.get(router, {}).get(source_id, [])
        if external_id in lst:
            lst.remove(external_id)
            self._save()
            logger.info("Sync-Ignore entfernt: %s/%s -> %s", router, source_id, external_id)


# ── Abstrakte Basis-Klasse für Sync-Adapter ───────────────────────────────────

class SyncAdapter(ABC):
    """
    Jede externe Datenquelle implementiert diesen Adapter.
    Aktuell: JiraSyncAdapter
    Künftig: AzureDevOpsSyncAdapter, GitHubProjectsSyncAdapter, …
    """

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def fetch_preview(self) -> SyncPreview:
        """
        Holt Daten von der externen Quelle, vergleicht mit der DB und
        liefert eine SyncPreview ohne die DB zu verändern.
        """

    @abstractmethod
    def apply(self, diffs: list[RecordDiff]) -> dict:
        """
        Wendet die übergebenen Diffs auf die lokale DB an.
        Gibt ein Ergebnisdict zurück: {"created": int, "updated": int, "errors": list}.
        """


# ── Registry & Factory ─────────────────────────────────────────────────────────

_ADAPTER_REGISTRY: dict[str, type[SyncAdapter]] = {}


def register_adapter(source_type: str):
    """Decorator zum Registrieren eines Adapters."""
    def decorator(cls: type[SyncAdapter]):
        _ADAPTER_REGISTRY[source_type] = cls
        logger.debug("Sync-Adapter registriert: %s → %s", source_type, cls.__name__)
        return cls
    return decorator


def get_adapter(source_config: dict) -> SyncAdapter:
    source_type = source_config.get("type", "").lower()
    cls = _ADAPTER_REGISTRY.get(source_type)
    if cls is None:
        raise ValueError(f"Kein Sync-Adapter für Typ '{source_type}' registriert.")
    return cls(source_config)


# ── Globale Instanzen ───────────────────────────────────────────────────────────
sync_config       = SyncConfig()
sync_ignore_store = SyncIgnoreStore()