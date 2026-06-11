# routers/planning.py
"""
routers/planning.py – Ressourcenzuordnung (Tabelle: planning)

DB-Schema:
planning(task_id, project_id, staff, role_id, start_date date, end_date date)
– start_date/end_date = Montag/Freitag der zugewiesenen Kalenderwoche
– Stunden werden NICHT gespeichert; sie ergeben sich aus staff.hours_per_day
  multipliziert mit den effektiven Arbeitstagen (abzgl. Feiertage + Abwesenheiten)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Set # Set hinzugefügt für effiziente Duplikatsprüfung bei IDs
from datetime import date, timedelta

import holidays

from db import get_cursor
from capacity import (
    calculate_capacity_per_week,
    iso_week_key,
    working_days_in_range,
    get_austrian_holidays,
)

router = APIRouter()

# ── Pydantic-Modelle ───────────────────────────────────────────────────────────

class PlanningEntry(BaseModel):
    task_id:       Optional[int] = None
    project_id:    Optional[int] = None
    staff:         str
    role_id:       int
    calendar_week: str   # Format: 'YYYY-WNN'

class PlanningDelete(BaseModel):
    staff:         str
    calendar_week: str
    project_id:    Optional[int] = None
    task_id:       Optional[int] = None

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _week_bounds(week_key: str):
    """Gibt (Montag, Freitag) der ISO-Kalenderwoche zurück."""
    year, week = int(week_key.split("-W")[0]), int(week_key.split("-W")[1])
    monday = date.fromisocalendar(year, week, 1)
    friday = monday + timedelta(days=4)
    return monday, friday

def _absent_workdays_in_week(shortname: str, week_key: str,
                             absences: list, at_hols: Set[date]) -> int:
    """Anzahl der Arbeitstage in der KW, an denen der MA abwesend ist."""
    monday, friday = _week_bounds(week_key)
    absent_days = set()
    for ab in absences:
        if ab["shortname"] != shortname:
            continue
        cur = max(ab["absence_from"], monday)
        end = min(ab["absence_to"],   friday)
        while cur <= end:
            if cur.weekday() < 5 and cur not in at_hols:
                absent_days.add(cur)
            cur += timedelta(days=1)
    return len(absent_days)

def _total_workdays_in_week(week_key: str, at_hols: Set[date]) -> int:
    """Gesamtzahl Arbeitstage (Mo–Fr, kein Feiertag) in der KW."""
    monday, friday = _week_bounds(week_key)
    count = 0
    cur = monday
    while cur <= friday:
        if cur.weekday() < 5 and cur not in at_hols:
            count += 1
        cur += timedelta(days=1)
    return count

def _is_majority_absent(shortname: str, week_key: str,
                        absences: list, at_hols: Set[date]) -> bool:
    """True wenn mehr als die Hälfte der Arbeitstage der KW Abwesenheit eingetragen ist."""
    total  = _total_workdays_in_week(week_key, at_hols)
    if total == 0:
        return True
    absent = _absent_workdays_in_week(shortname, week_key, absences, at_hols)
    return absent > total / 2

def _effective_hours_in_week(shortname: str, hours_per_day: float,
                             week_key: str, absences: list, at_hols: Set[date]) -> float:
    """
    Effektive Stunden des Mitarbeiters in der KW:
    hours_per_day × (Arbeitstage − Abwesenheitstage)
    """
    total  = _total_workdays_in_week(week_key, at_hols)
    absent = _absent_workdays_in_week(shortname, week_key, absences, at_hols)
    return max(0.0, (total - absent) * float(hours_per_day))

def _build_at_hols(start: date, end: date) -> Set[date]:
    years = set(range(start.year, end.year + 1))
    hols: Set[date] = set()
    for y in years:
        hols |= get_austrian_holidays(y)
    return hols

def _next_week_key(base_date: date) -> str:
    # Fügt 7 Tage hinzu, um ein Datum in der nächsten Woche zu erhalten,
    # dann konvertiert es in die ISO-Woche. Die ISO-Wochenberechnung
    # behandelt dies korrekt (z.B. Jahreswechsel).
    next_week_date = base_date + timedelta(weeks=1)
    nxt_iso = next_week_date.isocalendar()
    return f"{nxt_iso[0]}-W{nxt_iso[1]:02d}"

# ── GET / ──────────────────────────────────────────────────────────────────────

@router.get("/")
def get_planning(
    use_current_week: bool = False,
    end_week: Optional[str] = None,          # Format: 'YYYY-WNN'
    filter_project_ids:   Optional[str] = None,
    filter_project_names: Optional[str] = None,
    filter_task_ids:      Optional[str] = None,
    filter_task_names:    Optional[str] = None,
):
    """
    Vollständige Planungsmatrix.

    Startzeitpunkt:
    • use_current_week=True  → aktuelle KW
    • use_current_week=False → min(project.start_date) der geplanten, nicht
      erledigten Projekte; Fallback: aktuelle KW

    Endzeitpunkt: max(project.due_date) der geplanten, nicht erledigten Projekte;
    Fallback: 12 KW nach Start.

    Optionale Filter (für planning_status-Ansicht, können kommasepariert sein):
    filter_project_ids / filter_project_names – filtert Matrix auf ein oder mehrere Projekte
    filter_task_ids    / filter_task_names    – filtert Matrix auf einen oder mehrere Tasks
    """
    with get_cursor() as cur:
        # Initialisiere Sets für effektive IDs, um Duplikate zu vermeiden
        effective_project_ids_set: Set[int] = set()
        effective_task_ids_set: Set[int] = set()
        
        # Zur Erstellung der Überschrift im Frontend
        filter_title_parts: List[str] = []

        # 1. IDs direkt aus Parametern parsen
        if filter_project_ids:
            try:
                for _id_str in filter_project_ids.split(','):
                    effective_project_ids_set.add(int(_id_str.strip()))
                filter_title_parts.append(f"Projekte (IDs: {filter_project_ids})")
            except ValueError:
                raise HTTPException(422, f"Ungültige Projekt-ID(s): {filter_project_ids}")
        if filter_task_ids:
            try:
                for _id_str in filter_task_ids.split(','):
                    effective_task_ids_set.add(int(_id_str.strip()))
                filter_title_parts.append(f"Tasks (IDs: {filter_task_ids})")
            except ValueError:
                raise HTTPException(422, f"Ungültige Task-ID(s): {filter_task_ids}")

        # 2. Namen zu IDs auflösen
        if filter_project_names:
            names_to_search = tuple([name.strip() for name in filter_project_names.split(',') if name.strip()])
            if names_to_search:
                cur.execute("SELECT project_id, project_name FROM project WHERE project_name ILIKE ANY(%s)", (list(names_to_search),))
                found_projects = cur.fetchall()
                if not found_projects:
                    raise HTTPException(404, f"Keine Projekte mit Namen gefunden: {filter_project_names}")
                for proj in found_projects:
                    effective_project_ids_set.add(proj["project_id"])
                filter_title_parts.append(f"Projekte: {', '.join([p['project_name'] for p in found_projects])}")

        if filter_task_names:
            names_to_search = tuple([name.strip() for name in filter_task_names.split(',') if name.strip()])
            if names_to_search:
                cur.execute("SELECT task_id, task_name FROM tasks WHERE task_name ILIKE ANY(%s)", (list(names_to_search),))
                found_tasks = cur.fetchall()
                if not found_tasks:
                    raise HTTPException(404, f"Keine Tasks mit Namen gefunden: {filter_task_names}")
                for task in found_tasks:
                    effective_task_ids_set.add(task["task_id"])
                filter_title_parts.append(f"Tasks: {', '.join([t['task_name'] for t in found_tasks])}")

        effective_project_ids = list(effective_project_ids_set)
        effective_task_ids = list(effective_task_ids_set)
        
        # Fehlermeldung, wenn Filter angegeben wurden, aber nichts gefunden wurde
        if (filter_project_ids or filter_project_names or filter_task_ids or filter_task_names) and \
           (not effective_project_ids and not effective_task_ids):
            raise HTTPException(404, "Die angegebenen Projekte/Tasks konnten nicht gefunden werden oder existieren nicht.")

        # Ermittle zugehörige Projekt-IDs von gefilterten Tasks
        task_project_ids_set: Set[int] = set()
        if effective_task_ids:
            cur.execute("SELECT DISTINCT project_id FROM tasks WHERE task_id = ANY(%s) AND project_id IS NOT NULL", (effective_task_ids,))
            for row in cur.fetchall():
                task_project_ids_set.add(row["project_id"])

        # Kombiniere alle relevanten Projekt-IDs für die Zeitbereich-Berechnung und weitere Filter
        all_relevant_project_ids = list(effective_project_ids_set.union(task_project_ids_set))

        # ── Zeitbereich aus Projektdaten (angepasst für mehrere IDs) ────────────────
        range_query_sql = """
            SELECT MIN(start_date) AS min_start,
                   MAX(due_date)   AS max_due
            FROM project
            WHERE planned = TRUE AND done = FALSE
            AND project_type = 'Project'
            AND start_date IS NOT NULL AND due_date IS NOT NULL
        """
        range_query_params = []
        if all_relevant_project_ids:
            range_query_sql += " AND project_id = ANY(%s)"
            range_query_params.append(all_relevant_project_ids)

        cur.execute(range_query_sql, range_query_params)
        range_row = cur.fetchone()

        today      = date.today()
        today_iso  = today.isocalendar()
        cur_monday = date.fromisocalendar(today_iso[0], today_iso[1], 1)

        # Wenn use_current_week True ist, Start immer heute.
        # Im planning_status-Kontext ist use_current_week immer True.
        start_date = cur_monday

        # Für end_date, entweder max_due der gefilterten Projekte oder 12 Wochen nach start_date
        if range_row and range_row["max_due"]:
            md     = range_row["max_due"]
            md_iso = md.isocalendar()
            end_date = date.fromisocalendar(md_iso[0], md_iso[1], 7) # Freitag der KW
        else:
            end_date = start_date + timedelta(weeks=12) # Fallback if no projects or no due_date

        # ── Mitarbeiter mit Developer/Tester-Rolle ──────────────────────────
        cur.execute("""
            SELECT s.shortname, s.hours_per_day, s.is_active,
                   r.role, r.role_id
            FROM staff s
            JOIN roles r ON r.shortname = s.shortname
            WHERE r.role IN ('Developer', 'Tester')
            AND s.is_active = TRUE
            ORDER BY r.role ASC, s.shortname ASC
        """)
        staff_roles = cur.fetchall()
        shortnames = list({r["shortname"] for r in staff_roles})

        # ── Abwesenheiten im Zeitraum ───────────────────────────────────────
        if shortnames:
            cur.execute("""
                SELECT shortname, absence_from, absence_to, absence_type
                FROM absence
                WHERE shortname = ANY(%s)
                AND absence_to   >= %s
                AND absence_from <= %s
            """, (shortnames, start_date, end_date))
            absences = cur.fetchall()
        else:
            absences = []
        absences_list = [dict(a) for a in absences]

        # ── Bestehende Planungen laden (ggf. gefiltert) ─────────────────────
        sql_plannings = """
            SELECT pl.task_id, pl.project_id, pl.staff, pl.role_id,
                   pl.start_date, pl.end_date,
                   p.project_name, p.color_hexcode AS project_color,
                   t.task_name,    t.color_hexcode AS task_color
            FROM planning pl
            LEFT JOIN project p ON p.project_id = pl.project_id
            LEFT JOIN tasks   t ON t.task_id    = pl.task_id
            WHERE pl.end_date   >= %s
            AND   pl.start_date <= %s
        """
        params_plannings = [start_date, end_date]

        combined_planning_filter_parts = []
        combined_planning_params = []

        if effective_task_ids:
            # Planungen für spezifische Tasks (project_id kann NULL sein)
            combined_planning_filter_parts.append("pl.task_id = ANY(%s)")
            combined_planning_params.append(effective_task_ids)

        if effective_project_ids:
            # Planungen für spezifische Projekte ODER Tasks, die zu diesen Projekten gehören
            # (Diese Tasks müssen dann einen project_id in der tasks-Tabelle haben)
            combined_planning_filter_parts.append("(pl.project_id = ANY(%s) OR t.project_id = ANY(%s))")
            combined_planning_params.extend([effective_project_ids, effective_project_ids])
        
        # Wenn nur project_names oder project_ids als Filter gegeben wurden,
        # aber keine Task-Filter und ein Task trotzdem im relevanten Projektbereich
        # liegt, dann muss dieser Task auch angezeigt werden.
        # Die `t.project_id = ANY(%s)` in der obigen Bedingung deckt dies ab.

        if combined_planning_filter_parts:
            # Wir verwenden EINE ODER-Verknüpfung für alle Filterbedingungen,
            # damit Planungseinträge angezeigt werden, die mindestens eine der Bedingungen erfüllen.
            sql_plannings += " AND (" + " OR ".join(combined_planning_filter_parts) + ")"
            params_plannings.extend(combined_planning_params)

        cur.execute(sql_plannings, params_plannings)
        plannings_raw = [dict(r) for r in cur.fetchall()]

        # Wenn plannings_raw leer ist UND Filter angegeben wurden, bedeutet das, nichts gefunden.
        if not plannings_raw and (effective_project_ids or effective_task_ids):
            raise HTTPException(404, "Für die angegebenen Projekte/Tasks wurde keine Planung im sichtbaren Zeitraum gefunden.")


        # ── Verfügbare Projekte (Drag&Drop / Referenz - gefiltert für Lesemodus) ──────────────────────
        # Diese Liste wird nun auch durch die all_relevant_project_ids gefiltert.
        # Wichtig: Wenn nur Tasks gefiltert wurden, die keinem Projekt zugeordnet sind,
        # oder deren Projekte außerhalb des Zeitbereichs liegen, kann diese Liste leer sein.
        sql_all_projects = """
            SELECT p.project_id, p.project_name, p.customer, p.color_hexcode,
                   p.start_date, p.due_date, p.target_hours, p.impl_hours, p.test_hours
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE
            AND p.project_type = 'Project'
        """
        params_all_projects = []
        if all_relevant_project_ids:
            sql_all_projects += " AND p.project_id = ANY(%s)"
            params_all_projects.append(all_relevant_project_ids)

        sql_all_projects += " ORDER BY p.due_date ASC NULLS LAST, p.project_name ASC"
        cur.execute(sql_all_projects, params_all_projects)
        all_projects_filtered = cur.fetchall() # Um Verwechslung mit 'all_projects' weiter unten zu vermeiden

        # Letztes worked_hours-Datum je Projekt (für outdated-Markierung)
        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

        # ── Verfügbare Tasks (Drag&Drop / Referenz - gefiltert für Lesemodus) ──────────────────────────
        # Diese Liste wird auch durch die effektiv gefilterten Tasks oder zugehörigen Projekte gefiltert.
        sql_available_tasks = """
            SELECT t.task_id, t.task_name, t.color_hexcode, t.project_id,
                   p.project_name
            FROM tasks t
            LEFT JOIN project p ON p.project_id = t.project_id
        """
        params_available_tasks = []
        where_clauses_tasks = []

        if effective_task_ids:
            where_clauses_tasks.append("t.task_id = ANY(%s)")
            params_available_tasks.append(effective_task_ids)
        if all_relevant_project_ids: # Tasks, die zu den relevanten Projekten gehören (inkl. der task_project_ids)
            where_clauses_tasks.append("t.project_id = ANY(%s)")
            params_available_tasks.append(all_relevant_project_ids)

        if where_clauses_tasks:
            sql_available_tasks += " WHERE " + " OR ".join(where_clauses_tasks)

        sql_available_tasks += " ORDER BY t.task_name"
        cur.execute(sql_available_tasks, params_available_tasks)
        available_tasks = cur.fetchall()

        # ── Feiertage für gesamten Zeitraum ─────────────────────────────────
        at_hols = _build_at_hols(start_date, end_date)

        # ── Kalenderwochen-Liste aufbauen ────────────────────────────────────
        weeks: List[str] = []
        cur_d = start_date
        while cur_d <= end_date:
            wk = iso_week_key(cur_d)
            if not weeks or weeks[-1] != wk:
                weeks.append(wk)
            cur_d += timedelta(days=7)

        # ── Abwesenheits-Majoritäts-Map ──────────────────────────────────────
        # {shortname: {week_key: {'is_majority': bool, 'type': str, ...}}}
        absence_map: dict = {}
        for sr in staff_roles:
            name = sr["shortname"]
            absence_map[name] = {}
            for wk in weeks:
                absent_days = _absent_workdays_in_week(name, wk, absences_list, at_hols)
                total_days  = _total_workdays_in_week(wk, at_hols)
                if absent_days == 0:
                    continue

                # Abwesenheitstyp dieser Woche (erster Treffer)
                monday, friday = _week_bounds(wk)
                ab_type = "Abwesend"
                for ab in absences_list:
                    if (ab["shortname"] == name
                            and ab["absence_from"] <= friday
                            and ab["absence_to"]   >= monday):
                        ab_type = ab["absence_type"]
                        break
                absence_map[name][wk] = {
                    "absent_days": absent_days,
                    "total_days":  total_days,
                    "is_majority": absent_days > total_days / 2,
                    "type":        ab_type,
                }

        # ── Kapazität pro Mitarbeiter + KW ───────────────────────────────────
        # Stunden = hours_per_day × effektive Arbeitstage (ohne Feiertage, ohne Abwesenheit)
        capacity_by_staff: dict = {}
        capacity_totals:   dict = {wk: 0.0 for wk in weeks}

        staff_hpd_map = {r["shortname"]: float(r["hours_per_day"]) for r in staff_roles} # Korrigierter Typo

        for name, hpd in staff_hpd_map.items():
            week_hours: dict = {}
            for wk in weeks:
                h = _effective_hours_in_week(name, hpd, wk, absences_list, at_hols)
                week_hours[wk] = h
                capacity_totals[wk] += h # Korrigierte Platzierung
            capacity_by_staff[name] = {
                "hours_per_day": hpd,
                "week_hours":    week_hours,
            }

        # ── end_week: Enddatum der sichtbaren Matrix begrenzen ───────────────
        # In planning_status wird end_week nicht vom Frontend gesendet.
        # Diese Logik bleibt für die Haupt-planning.html bestehen, hat aber keinen Effekt für planning_status.
        if end_week:
            weeks = [wk for wk in weeks if wk <= end_week]
            # available_projects wird hier nicht mehr direkt gekürzt, da es bereits gefiltert wurde.


        # ── Outdated-Flag in plan_map setzen ─────────────────────────────────
        # Eine Planungszeile gilt als "veraltet" wenn es für das zugehörige Projekt
        # bereits worked_hours-Einträge gibt, deren day NACH dem end_date der Planung liegt.
        # Solche Zeilen werden in der Matrix farblich gekennzeichnet und in project_status
        # nicht als offene Planstunden eingerechnet.
        plan_map: dict = {}
        for pl in plannings_raw:
            wk    = iso_week_key(pl["start_date"])
            pid   = pl["project_id"]
            entry = dict(pl)

            # Outdated: hat das Projekt neuere worked_hours als diese Planung?
            entry["is_outdated"] = (
                pid is not None
                and pid in max_worked_day
                and max_worked_day[pid] > pl["end_date"]
            )
            plan_map.setdefault(pl["staff"], {}).setdefault(wk, []).append(entry)

        return {
            "weeks":              weeks,
            "staff_roles":        [dict(r) for r in staff_roles],
            "capacity_totals":    capacity_totals,
            "capacity_by_staff":  capacity_by_staff,
            "plannings":          plan_map,
            "absence_map":        absence_map,
            "available_projects": [dict(p) for p in all_projects_filtered], # gefilterte Liste verwenden
            "available_tasks":    [dict(t) for t in available_tasks], # gefilterte Liste verwenden
            "filter_title_parts": filter_title_parts, # Für die dynamische Überschrift
        }

# ── POST /assign ───────────────────────────────────────────────────────────────

@router.post("/assign")
def assign_planning(data: PlanningEntry):
    """
    Projekt oder Task einem Mitarbeiter für eine KW zuweisen.
    Speichert start_date (Montag) und end_date (Freitag) der KW in der DB.
    """
    if data.task_id is None and data.project_id is None:
        raise HTTPException(422, "task_id oder project_id muss angegeben sein")

    monday, friday = _week_bounds(data.calendar_week)

    # Abwesenheitsprüfung: Mehrheit der Arbeitstage abwesend?
    with get_cursor() as cur:
        cur.execute("""
            SELECT shortname, absence_from, absence_to, absence_type
            FROM absence WHERE shortname = %s
            AND absence_to >= %s AND absence_from <= %s
        """, (data.staff, monday, friday))
        absences = [dict(r) for r in cur.fetchall()]

    at_hols = _build_at_hols(monday, friday)
    if _is_majority_absent(data.staff, data.calendar_week, absences, at_hols):
        raise HTTPException(409,
            "Mitarbeiter ist in dieser Woche mehrheitlich abwesend – Zuweisung nicht möglich")

    with get_cursor(commit=True) as cur:

        # Prüfen ob für diesen Mitarbeiter in dieser KW bereits ein Eintrag existiert
        # (egal welche Rolle) – ein MA kann pro Woche nur einmal eingeplant werden
        cur.execute("""
            SELECT role_id, project_id, task_id FROM planning
            WHERE staff = %s AND start_date = %s
        """, (data.staff, monday))
        existing = cur.fetchone()

        if existing:
            if existing["role_id"] != data.role_id:
                # Anderer Rollen-Eintrag existiert bereits → ablehnen
                raise HTTPException(409,
                    "Mitarbeiter ist in dieser Woche bereits als andere Rolle eingeplant")

            # Gleiche Rolle → alten Eintrag ersetzen
            cur.execute("""
                DELETE FROM planning WHERE staff = %s AND start_date = %s
            """, (data.staff, monday))

        cur.execute("""
            INSERT INTO planning (task_id, project_id, staff, role_id, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (data.task_id, data.project_id, data.staff,
              data.role_id, monday, friday))

    return {"status": "ok", "start_date": str(monday), "end_date": str(friday)}

# ── DELETE /remove ─────────────────────────────────────────────────────────────

@router.delete("/remove")
def remove_planning(data: PlanningDelete):
    """Planungszuordnung für eine KW entfernen."""
    monday, _ = _week_bounds(data.calendar_week)

    with get_cursor(commit=True) as cur:
        sql    = "DELETE FROM planning WHERE staff = %s AND start_date = %s"
        params: list = [data.staff, monday]

        if data.project_id is not None:
            sql += " AND project_id = %s"
            params.append(data.project_id)
        if data.task_id is not None:
            sql += " AND task_id = %s"
            params.append(data.task_id)

        cur.execute(sql, params)

    return {"status": "ok"}

# ── GET /project_status ────────────────────────────────────────────────────────

@router.get("/project_status")
def project_planning_status():
    """
    Planungsstatus je aktivem Projekt:
    • geplante Stunden aus planning (hours_per_day × effektive Tage je KW)
    • abzgl. worked_hours
    • Differenz + Farbampel
    • Ist-Liefertermin (letzte KW mit Planung, unter Berücksichtigung der 15h-Regel)
    """

    # ── Alle DB-Abfragen in EINEM cursor-Block ─────────────────────────────────
    # Dies behebt den "cursor already closed" Fehler. Alle DB-Zugriffe erfolgen innerhalb
    # dieses einzigen 'with'-Blocks, bevor weitere Berechnungen stattfinden.
    with get_cursor() as cur:

        # Projekte laden
        cur.execute("""
            SELECT p.project_id, p.project_name, p.customer,
                   p.target_hours, p.impl_hours AS plan_impl,
                   p.test_hours   AS plan_test,
                   p.due_date, p.color_hexcode
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE
            AND p.project_type = 'Project'
            ORDER BY p.due_date ASC NULLS LAST, p.project_name ASC
        """)
        projects = cur.fetchall()

        # Geleistete Stunden je Projekt
        cur.execute("""
            SELECT project_id,
                   COALESCE(SUM(impl_hours),0) AS worked_impl,
                   COALESCE(SUM(test_hours),0) AS worked_test
            FROM worked_hours GROUP BY project_id
        """)
        worked = {r["project_id"]: r for r in cur.fetchall()}

        # Planungseinträge mit Mitarbeiter-Stunden je Woche
        cur.execute("""
            SELECT pl.project_id, pl.task_id,
                   pl.staff, pl.start_date, pl.end_date,
                   r.role,
                   s.hours_per_day
            FROM planning pl
            JOIN roles r ON r.role_id   = pl.role_id
            JOIN staff s ON s.shortname = pl.staff
            WHERE pl.project_id IS NOT NULL
        """)
        plan_rows = [dict(r) for r in cur.fetchall()]

        # Abwesenheiten für beteiligte Mitarbeiter im relevanten Zeitraum
        # Die min_d/max_d müssen für die Feiertags-Berechnung einen sinnvollen Bereich abdecken.
        # Fallback auf heute, wenn keine Planungszeilen existieren.
        if plan_rows:
            pstaff = list({r["staff"] for r in plan_rows})
            min_d  = min(r["start_date"] for r in plan_rows)
            max_d  = max(r["end_date"]   for r in plan_rows)
            cur.execute("""
                SELECT shortname, absence_from, absence_to
                FROM absence
                WHERE shortname = ANY(%s)
                AND absence_to >= %s AND absence_from <= %s
            """, (pstaff, min_d, max_d))
            all_absences = [dict(a) for a in cur.fetchall()]
        else:
            all_absences = []
            min_d = date.today()
            max_d = date.today()

        # Letztes worked_hours-Datum je Projekt (für outdated-Markierung)
        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day_status = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

    # ── Ab hier: reine Python-Berechnungen, kein Cursor mehr nötig ────────────

    # Feiertage für den gesamten Betrachtungszeitraum aufbauen
    # Erweiterung um einen Puffer von 12 Wochen, um auch zukünftige Ist-KW bestimmen zu können.
    at_hols = _build_at_hols(min_d, max_d + timedelta(weeks=12))

    # Geplante Stunden je Projekt + Rolle aggregieren (veraltete Einträge werden NICHT eingerechnet)
    plan_agg:     dict = {}   # {project_id: {'Developer': float, 'Tester': float}}
    last_end_map: dict = {}   # {project_id: date}  – Ende der letzten NICHT-VERALTETEN Planung

    for pr in plan_rows:
        pid = pr["project_id"]

        # Outdated-Check: gibt es worked_hours mit day > pl.end_date?
        is_outdated = (
            pid in max_worked_day_status
            and max_worked_day_status[pid] > pr["end_date"]
        )
        if is_outdated:
            continue  # Diese veraltete Planung nicht in die offenen Stunden einrechnen

        wk = iso_week_key(pr["start_date"])
        h  = _effective_hours_in_week(
            pr["staff"], float(pr["hours_per_day"]),
            wk, all_absences, at_hols)

        plan_agg.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
        role_key = pr["role"] if pr["role"] in ("Developer", "Tester") else "Developer"
        plan_agg[pid][role_key] += h

        # Aktualisiere das Enddatum der letzten NICHT-VERALTETEN Planung für dieses Projekt
        prev_end_date = last_end_map.get(pid)
        if prev_end_date is None or pr["end_date"] > prev_end_date:
            last_end_map[pid] = pr["end_date"]

    # Ergebnisliste aufbauen
    result = []
    for p in projects:
        pid = p["project_id"]
        w   = worked.get(pid, {"worked_impl": 0, "worked_test": 0})
        pa  = plan_agg.get(pid, {"Developer": 0.0, "Tester": 0.0})

        # 'diff' ist die entscheidende Metrik für die "offenen Stunden"
        # (berechnet aus geplanten Zielstunden abzgl. geleisteter Stunden und bereits verplanter Stunden)
        remaining_impl = p["plan_impl"] - float(w["worked_impl"]) - pa["Developer"]
        remaining_test = p["plan_test"] - float(w["worked_test"]) - pa["Tester"]
        diff           = remaining_impl + remaining_test

        # 'restaufwand' ist die gesamte verbleibende Arbeit basierend auf target_hours und worked_hours
        # (ohne Berücksichtigung zukünftiger Planungen)
        restaufwand = (
            p["target_hours"]
            - float(w["worked_impl"])
            - float(w["worked_test"])
        )

        status_color      = ""
        ist_kw_calculated = None

        # --- LOGIK FÜR STATUSFARBE UND IST_KW ---
        # Bedingung: wenn 'offene Stunden' (diff) <= 0 ODER positiv aber unter 15h
        if diff <= 0 or (diff > 0 and diff < 15):
            status_color = "lightgreen"

            # Der "Liefertermin Ist" soll die KW NACH der letzten bekannten Planung sein.
            # Falls KEINE Planung für das Projekt existiert (last_end_map leer),
            # nehmen wir das heutige Datum als Referenz für "nächste Woche".
            base_date_for_next_week = last_end_map.get(pid, date.today())
            ist_kw_calculated = _next_week_key(base_date_for_next_week)
        else:
            # diff >= 15: echter, signifikanter Restaufwand der noch nicht abgedeckt ist
            status_color      = "red"
            ist_kw_calculated = None

        # Soll-KW aus due_date (wird als Fallback für ist_kw verwendet)
        due_kw = None
        if p["due_date"]:
            iso    = p["due_date"].isocalendar()
            due_kw = f"{iso[0]}-W{iso[1]:02d}"

        # Endgültige Zuweisung für ist_kw:
        # Priorität 1: Der eben berechnete ist_kw_calculated (wenn nicht None)
        # Priorität 2: Der Soll-Liefertermin (due_kw) als Fallback
        final_ist_kw = ist_kw_calculated if ist_kw_calculated is not None else due_kw

        result.append({
            "project_id":      pid,
            "project_name":    p["project_name"],
            "customer":        p["customer"],
            "color_hexcode":   p["color_hexcode"],
            "target_hours":    p["target_hours"],
            "restaufwand":     restaufwand,
            "due_date":        str(p["due_date"]) if p["due_date"] else None,
            "due_kw":          due_kw,
            "ist_kw":          final_ist_kw,
            "remaining_impl":  remaining_impl,
            "remaining_test":  remaining_test,
            "remaining_hours": diff,
            "status_color":    status_color,
        })

    return result