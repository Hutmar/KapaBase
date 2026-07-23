"""
capacity.py – Business-Logik für Kapazitätsberechnungen
Verwendet:
- python3-holidays  (ISO-8601-konforme Feiertagsberechnung für Österreich)
- config.json (Schlüssel "additional_holidays") für zusätzliche, unternehmens-
  spezifische Feiertage, die ergänzend zu den offiziellen österreichischen
  Feiertagen berücksichtigt werden sollen (z.B. Betriebsurlaub, Fenstertage).
- Eigene DB-Abfragen für Abwesenheiten und Mitarbeiter-Stunden
"""
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from decimal import Decimal  # Importiert das Decimal-Modul für präzise Berechnungen
import json
import logging
import holidays
from db import get_cursor

logger = logging.getLogger(__name__)

# ── Zusätzliche Feiertage aus config.json ──────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.json"

# Einfacher, mtime-basierter Cache: Änderungen an config.json wirken sich
# ohne Neustart der Anwendung aus, die Datei wird aber nicht bei jedem
# Aufruf neu von der Platte gelesen.
_additional_holidays_cache: Optional[set] = None
_additional_holidays_mtime: Optional[float] = None


def _load_additional_holidays() -> set:
    """
    Liest zusätzliche (unternehmensspezifische) Feiertage aus config.json,
    Schlüssel "additional_holidays" – eine einfache Liste von
    ISO-Datumsstrings ("YYYY-MM-DD"), z.B.:

        {
          "additional_holidays": ["2026-05-02", "2026-12-24", "2026-12-31"]
        }

    Diese Tage werden zusätzlich zu den offiziellen österreichischen
    Feiertagen (siehe get_austrian_holidays()) als Feiertage behandelt –
    und zwar überall dort, wo Arbeitstage berechnet werden (Kapazität,
    Abwesenheiten, Restaufwand-/Liefertermin-Berechnungen usw.), da all
    diese Stellen letztlich über get_austrian_holidays() bzw.
    working_days_in_range() laufen.
    """
    global _additional_holidays_cache, _additional_holidays_mtime

    if not CONFIG_PATH.exists():
        return set()

    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return _additional_holidays_cache or set()

    if _additional_holidays_cache is not None and mtime == _additional_holidays_mtime:
        return _additional_holidays_cache

    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(
            "Konnte config.json nicht lesen (%s) – additional_holidays wird ignoriert.", exc
        )
        return _additional_holidays_cache or set()

    raw_list = data.get("additional_holidays", []) or []
    parsed: set = set()
    for item in raw_list:
        try:
            parsed.add(date.fromisoformat(str(item)[:10]))
        except ValueError:
            logger.warning(
                "Ungültiges Datum in additional_holidays der config.json: %r – wird ignoriert.",
                item,
            )

    _additional_holidays_cache = parsed
    _additional_holidays_mtime = mtime
    return parsed


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────
def get_austrian_holidays(year: int) -> set:
    """
    Gibt alle österreichischen Feiertage (Wien = Gesamtösterreich) für ein
    Jahr zurück, ERGÄNZT um die in config.json unter "additional_holidays"
    hinterlegten zusätzlichen Feiertage (auf das angegebene Jahr gefiltert).

    Da praktisch alle Arbeitstage-/Kapazitätsberechnungen der Anwendung
    (Projekte, Planung, Abwesenheiten, Restaufwand/Liefertermin) über diese
    Funktion bzw. working_days_in_range() laufen, wirkt sich ein Eintrag in
    additional_holidays automatisch überall aus.
    """
    try:
        official = set(holidays.Austria(subdiv="W", years=year).keys())
    except TypeError:
        # Alte API (< 0.11)
        official = set(holidays.Austria(prov="W", years=year).keys())

    additional_for_year = {d for d in _load_additional_holidays() if d.year == year}
    return official | additional_for_year


def is_working_day(d: date, at_holidays: set) -> bool:
    """True wenn d ein Arbeitstag (Mo–Fr, kein Feiertag) ist."""
    return d.weekday() < 5 and d not in at_holidays  

def working_days_in_range(start: date, end: date) -> List[date]:
    """Alle Arbeitstage (Mo–Fr, kein österr. Feiertag / zusätzlicher Feiertag) im geschlossenen Intervall [start, end]."""
    years = set(range(start.year, end.year + 1))
    at_hols: set = set()
    for y in years:
        at_hols |= get_austrian_holidays(y)  
    days = []
    current = start
    while current <= end:
        if is_working_day(current, at_hols):
            days.append(current)
        current += timedelta(days=1)
    return days  

def iso_week_key(d: date) -> str:
    """Gibt 'YYYY-WNN' zurück, z. B. '2025-W03'."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"  

def week_monday(year: int, week: int) -> date:
    """Montag der angegebenen ISO-Kalenderwoche."""
    return date.fromisocalendar(year, week, 1)  

def week_sunday(year: int, week: int) -> date:
    """Sonntag der angegebenen ISO-Kalenderwoche."""
    return date.fromisocalendar(year, week, 7)

# ── Kapazität gesamt ───────────────────────────────────────────────────────────  
def calculate_total_capacity(start: date, end: date,
                             active_only: bool = True) -> Dict:
    """
    Berechnet die Gesamtkapazität aller (aktiven) Mitarbeiter im Zeitraum [start, end].
    Zieht Feiertage (inkl. additional_holidays) und Abwesenheiten ab. Doppelbelegungen
    (z.B. Urlaub am Feiertag) werden dank Set-Operationen nicht mehrfach abgezogen.
    Zusätzlich werden Informationen über die Anzahl der Arbeitstage und Feiertage im Zeitraum zurückgegeben.
    """
    with get_cursor() as cur:
        # Alle aktiven Mitarbeiter mit ihren Tagesstunden
        filter_sql = "WHERE is_active = TRUE" if active_only else ""
        cur.execute(f"SELECT shortname, hours_per_day FROM staff {filter_sql}")
        staff_list = cur.fetchall()  
        if not staff_list:
            return {
                "total_hours": Decimal(0),
                "by_staff": {},
                "working_days_in_period": 0,
                "holidays_in_period": 0,
                "period_length_days": 0
            }  
        shortnames = [s["shortname"] for s in staff_list]

        # Abwesenheiten im Zeitraum laden
        cur.execute("""
            SELECT shortname, absence_from, absence_to
            FROM absence
            WHERE shortname = ANY(%s)
            AND absence_to   >= %s
            AND absence_from <= %s
        """, (shortnames, start, end))
        absences = cur.fetchall()

        # Abwesenheitstage pro Mitarbeiter als Set aufbauen
        absent_days: Dict[str, set] = {s: set() for s in shortnames}
        for ab in absences:
            # Begrenze die Abwesenheitstage auf das Berechnungsfenster [start, end]
            cur_d = max(ab["absence_from"], start)
            end_d = min(ab["absence_to"], end)

            # Sicherstellen, dass wir mit Kopien arbeiten und keine Endlosschleife triggern
            while cur_d <= end_d:
                absent_days[ab["shortname"]].add(cur_d)
                cur_d += timedelta(days=1)

        # Arbeitstage im Zeitraum ermitteln (working_days_in_range filtert Sa/So, AT-Feiertage
        # und additional_holidays bereits raus!)
        # Diese Menge enthält bereits nur Arbeitstage, die keine Feiertage sind.
        work_days = set(working_days_in_range(start, end))  

        # Berechnung der zusätzlichen Metriken für die Kapazitätsübersicht
        years_in_range = set(range(start.year, end.year + 1))
        all_austrian_holidays = set()
        for y in years_in_range:
            all_austrian_holidays |= get_austrian_holidays(y)

        period_length_days = (end - start).days + 1
        holidays_in_period = {d for d in all_austrian_holidays if start <= d <= end}
        working_days_not_holidays_count = len(work_days) # Dies ist die Anzahl der Arbeitstage, die keine Feiertage sind.
        
        by_staff = {}
        total = Decimal(0)  # Als Decimal initialisieren  

        for s in staff_list:
            name = s["shortname"]
            # Sicherstellung der Decimal-Typisierung direkt beim Abruf, Konvertierung über String für Genauigkeit
            hpd = Decimal(str(s["hours_per_day"])) 

            # Echte Arbeitstage = Kalender-Arbeitstage minus Abwesenheiten an Arbeitstagen
            effective_days = work_days - absent_days[name]  
            
            hours = Decimal(len(effective_days)) * hpd
            by_staff[name] = float(hours) # Für die Ausgabe in JSON kann float akzeptabel sein
            total += hours  
        
        # Rückgabe der berechneten Werte, total_hours als float für JSON-Kompatibilität, aber intern Decimal verwendet
        return {
            "total_hours": float(total),  
            "by_staff": by_staff,
            "working_days_in_period": working_days_not_holidays_count, # Hinzugefügt
            "holidays_in_period": len(holidays_in_period), # Hinzugefügt
            "period_length_days": period_length_days # Hinzugefügt
        }  

def calculate_capacity_per_week(start: date, end: date,
                                active_only: bool = True,
                                roles_filter: Optional[List[str]] = None
                                ) -> Dict[str, Dict]:
    """
    Kapazität pro ISO-Kalenderwoche und Mitarbeiter.
    Optionaler Filter nach Rollen (z. B. ['Developer', 'Tester']).
    Rückgabe: {
    'weeks': ['2025-W03', ...],
    'by_staff': { shortname: {'role': ..., 'hours_per_week': {week_key: float}} },
    'totals': {week_key: float}
    }
    """
    with get_cursor() as cur:
        filter_sql = "WHERE s.is_active = TRUE" if active_only else "WHERE 1=1"
        role_join = ""
        role_params: list = []
        if roles_filter:
            role_join = """
            JOIN roles r ON r.shortname = s.shortname
            AND r.role::text = ANY(%s)
            """
            role_params = [roles_filter]
            filter_sql += ""  # already has WHERE  
        cur.execute(f"""
            SELECT DISTINCT s.shortname, s.hours_per_day
            FROM staff s
            {role_join}
            {filter_sql}
            ORDER BY s.shortname
        """, role_params if role_params else [])
        staff_list = cur.fetchall()  
        if not staff_list:
            return {"weeks": [], "by_staff": {}, "totals": {}}  
        shortnames = [s["shortname"] for s in staff_list]  
        cur.execute("""
            SELECT shortname, absence_from, absence_to
            FROM absence
            WHERE shortname = ANY(%s)
            AND absence_to   >= %s
            AND absence_from <= %s
        """, (shortnames, start, end))
        absences = cur.fetchall()

        # Rollen laden wenn kein Filter gesetzt
        cur.execute("""
            SELECT shortname, role FROM roles WHERE shortname = ANY(%s)
        """, (shortnames,))
        roles_rows = cur.fetchall()

        # Rollen-Map aufbauen (nimm erste Rolle)
        roles_map: Dict[str, str] = {}
        for r in roles_rows:
            if r["shortname"] not in roles_map:
                roles_map[r["shortname"]] = r["role"]

        # Abwesenheitstage pro Mitarbeiter
        absent_days: Dict[str, set] = {s: set() for s in shortnames}
        for ab in absences:
            cur_d = max(ab["absence_from"], start)
            end_d = min(ab["absence_to"], end)
            while cur_d <= end_d:
                absent_days[ab["shortname"]].add(cur_d)
                cur_d += timedelta(days=1)

        # Arbeitstage gruppiert nach KW (inkl. additional_holidays via get_austrian_holidays)
        years = set(range(start.year, end.year + 1))
        at_hols: set = set()
        for y in years:
            at_hols |= get_austrian_holidays(y)  
        week_days: Dict[str, List[date]] = {}
        current = start
        while current <= end:
            if is_working_day(current, at_hols):
                wk = iso_week_key(current)
                week_days.setdefault(wk, []).append(current)
            current += timedelta(days=1)  
        all_weeks = sorted(week_days.keys())  
        by_staff: Dict[str, Dict] = {}
        totals: Dict[str, float] = {wk: 0.0 for wk in all_weeks}  
        for s in staff_list:
            name = s["shortname"]
            hpd = Decimal(str(s["hours_per_day"])) # Auch hier Decimal verwenden
            week_hours: Dict[str, float] = {}
            for wk, days in week_days.items():
                eff = [d for d in days if d not in absent_days[name]]
                h = float(Decimal(len(eff)) * hpd) # Berechnung mit Decimal, dann zu float
                week_hours[wk] = h
                totals[wk] += h  
            by_staff[name] = {
                "role": roles_map.get(name, "Other"),
                "hours_per_day": hpd, # Könnte als Decimal zurückgegeben werden
                "week_hours": week_hours,
            }  
        return {"weeks": all_weeks, "by_staff": by_staff, "totals": totals}  

def is_fully_absent(shortname: str, week_key: str) -> bool:
    """
    Prüft ob ein Mitarbeiter für die gesamte Kalenderwoche abwesend ist.
    """
    parts = week_key.split("-W")
    year, week = int(parts[0]), int(parts[1])
    monday = week_monday(year, week)
    friday = monday + timedelta(days=4)  
    with get_cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) as cnt FROM absence
            WHERE shortname = %s
            AND absence_from <= %s
            AND absence_to   >= %s
        """, (shortname, monday, friday))
        row = cur.fetchone()
        return row["cnt"] > 0