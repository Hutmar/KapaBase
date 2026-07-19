"""
routers/config.py – Zentrale Anwendungs-Konfiguration (config.json)
=====================================================================
Liest Key-Values, gruppiert nach Themen, aus config.json (analog zu
sync.json / sync_engine.py) und stellt Endpunkte zum Abrufen bereit.

Struktur von config.json:
{
    "release": { "start_month": 5, "start_day": 1, "end_month": 3, "end_day": 31 },
    "<weitere_gruppe>": { ... }
}
"""

import json
import logging
from pathlib import Path
from datetime import date
from typing import Optional, Tuple
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


class AppConfig:
    """Liest und hält die Konfiguration aus config.json."""

    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning("config.json nicht gefunden: %s", self._path)
            self._data = {}
            return
        with open(self._path, encoding="utf-8") as fh:
            self._data = json.load(fh)
        logger.info("config.json geladen (%s)", self._path)

    def reload(self) -> None:
        """Konfiguration neu von der Festplatte laden."""
        self._load()

    def get_group(self, group: str) -> dict:
        return self._data.get(group, {})

    def get(self, group: str, key: str, default=None):
        return self._data.get(group, {}).get(key, default)


# ── Globale Instanz ─────────────────────────────────────────────────────────
app_config = AppConfig()


def get_current_release_range() -> Tuple[date, date]:
    """
    Berechnet den Zeitraum der aktuellen Release anhand der in config.json
    hinterlegten Werte (Gruppe "release"): vom start_day.start_month
    (z.B. 1.5.) bis end_day.end_month des Folgejahres (z.B. 31.3.).

    Liegt "heute" bereits nach dem Start-Datum dieses Jahres, läuft die
    aktuelle Release von diesem Jahr bis zum Ende-Datum im nächsten Jahr.
    Andernfalls (heute liegt vor dem Start-Datum dieses Jahres, z.B. im
    Zeitraum Jan–Apr) läuft die aktuelle Release vom Vorjahr bis zum
    Ende-Datum dieses Jahres.
    """
    rel = app_config.get_group("release")
    start_month = int(rel.get("start_month", 5))
    start_day   = int(rel.get("start_day", 1))
    end_month   = int(rel.get("end_month", 3))
    end_day     = int(rel.get("end_day", 31))

    today = date.today()
    start_this_year = date(today.year, start_month, start_day)

    if today >= start_this_year:
        release_start = start_this_year
        release_end   = date(today.year + 1, end_month, end_day)
    else:
        release_start = date(today.year - 1, start_month, start_day)
        release_end   = date(today.year, end_month, end_day)

    return release_start, release_end

def get_current_fiscal_year_range() -> Tuple[date, date]:
    """
    Berechnet den Zeitraum des aktuellen Wirtschaftsjahres anhand der in
    config.json hinterlegten Werte (Gruppe "fiscal_year"): Start-Monat/-Tag
    des WJ-Wechsels, Ende = Tag vor dem nächsten WJ-Wechsel.

    Liegt "heute" bereits nach dem Start-Datum dieses Jahres, läuft das
    aktuelle WJ von diesem Jahr bis zum Tag vor dem Start-Datum im
    nächsten Jahr. Andernfalls läuft das aktuelle WJ vom Vorjahr bis zum
    Tag vor dem Start-Datum dieses Jahres.
    """
    fy = app_config.get_group("fiscal_year")
    start_month = int(fy.get("start_month", 1))
    start_day   = int(fy.get("start_day", 1))

    today = date.today()
    start_this_year = date(today.year, start_month, start_day)

    if today >= start_this_year:
        fy_start = start_this_year
        fy_end   = date(today.year + 1, start_month, start_day) - timedelta(days=1)
    else:
        fy_start = date(today.year - 1, start_month, start_day)
        fy_end   = start_this_year - timedelta(days=1)

    return fy_start, fy_end
    
# ── Endpunkte ────────────────────────────────────────────────────────────────

@router.get("/")
def get_all_config():
    """Gesamte Konfiguration (alle Themen-Gruppen) zurückgeben."""
    return app_config._data


@router.get("/release/current")
def get_current_release():
    """Start-/Enddatum der aktuell laufenden Release."""
    start, end = get_current_release_range()
    return {"start_date": str(start), "end_date": str(end)}

@router.get("/fiscal_year/current")
def get_current_fiscal_year():
    """Start-/Enddatum des aktuell laufenden Wirtschaftsjahres."""
    start, end = get_current_fiscal_year_range()
    return {"start_date": str(start), "end_date": str(end)}
    
@router.get("/{group}")
def get_config_group(group: str):
    data = app_config.get_group(group)
    if not data:
        raise HTTPException(status_code=404, detail=f"Konfigurationsgruppe '{group}' nicht gefunden.")
    return data


@router.post("/reload")
def reload_config():
    """Konfiguration neu von der Festplatte laden (z.B. nach manueller Änderung)."""
    app_config.reload()
    return {"status": "ok"}