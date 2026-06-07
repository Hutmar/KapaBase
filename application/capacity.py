"""
capacity.py – Business-Logik für Kapazitätsberechnungen
Verwendet:
  - python3-holidays  (ISO-8601-konforme Feiertagsberechnung für Österreich)
  - Eigene DB-Abfragen für Abwesenheiten und Mitarbeiter-Stunden
"""

from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
from decimal import Decimal
import holidays

from db import get_cursor


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def get_austrian_holidays(year: int) -> set:
    """Gibt alle österreichischen Feiertage (Wien = Gesamtösterreich) für ein Jahr zurück."""
    return set(holidays.Austria(subdiv="W", years=year).keys())


def is_working_day(d: date, at_holidays: set) -> bool:
    """True wenn d ein Arbeitstag (Mo–Fr, kein Feiertag) ist."""
    return d.weekday() < 5 and d not in at_holidays


def working_days_in_range(start: date, end: date) -> List[date]:
    """Alle Arbeitstage (Mo–Fr, kein österr. Feiertag) im geschlossenen Intervall [start, end]."""
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
    Zieht Feiertage und Abwesenheiten ab. Doppelbelegungen (z.B. Urlaub am Feiertag) 
    werden dank Set-Operationen nicht mehrfach abgezogen.
    """
    with get_cursor() as cur:
        # Alle aktiven Mitarbeiter mit ihren Tagesstunden
        filter_sql = "WHERE is_active = TRUE" if active_only else ""
        cur.execute(f"SELECT shortname, hours_per_day FROM staff {filter_sql}")
        staff_list = cur.fetchall()

        if not staff_list:
            return {"total_hours": 0.0, "by_staff": {}}

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

    # Arbeitstage im Zeitraum ermitteln (working_days_in_range filtert Sa/So und AT-Feiertage bereits raus!)
    work_days = set(working_days_in_range(start, end))

    by_staff = {}
    total = 0.0  # Als float initialisieren, um Typenkonflikte zu vermeiden
    
    for s in staff_list:
        name = s["shortname"]
        hpd = float(s["hours_per_day"]) # Absicherung gegen PostgreSQL 'Numeric/Decimal'-Typen
        
        # Echte Arbeitstage = Kalender-Arbeitstage minus Abwesenheiten an Arbeitstagen
        effective_days = work_days - absent_days[name]
        
        hours = len(effective_days) * hpd
        by_staff[name] = hours
        total += hours

    return {"total_hours": total, "by_staff": by_staff}


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

    # Arbeitstage gruppiert nach KW
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
        hpd = s["hours_per_day"]
        week_hours: Dict[str, float] = {}
        for wk, days in week_days.items():
            eff = [d for d in days if d not in absent_days[name]]
            h = float(len(eff) * hpd)
            week_hours[wk] = h
            totals[wk] += h

        by_staff[name] = {
            "role": roles_map.get(name, "Other"),
            "hours_per_day": hpd,
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