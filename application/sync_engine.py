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


# ── Globale Config-Instanz ─────────────────────────────────────────────────────
sync_config = SyncConfig()
