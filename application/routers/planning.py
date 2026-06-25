# routers/planning.py
"""
routers/planning.py – Ressourcenzuordnung (Tabelle: planning)

DB-Schema:
planning(planning_id, task_id, project_id, staff, role_id, variant_id, start_date date, end_date date)
– start_date/end_date = Montag/Freitag der zugewiesenen Kalenderwoche
  (bzw. ein Teilbereich davon, siehe unten)
– Stunden werden NICHT gespeichert; sie ergeben sich aus staff.hours_per_day
  multipliziert mit den effektiven Arbeitstagen (abzgl. Feiertage + Abwesenheiten)

Zwei Projekte pro Mitarbeiter/Rolle/KW:
– Wird einem Mitarbeiter ein zweites Projekt für dieselbe Kalenderwoche
  zugewiesen, wird die Woche aufgeteilt: der bestehende Eintrag wird auf
  Montag–Mittwoch verkürzt, der neue Eintrag erhält Donnerstag–Freitag.
– Wird einer der beiden Einträge wieder gelöscht, wird der verbleibende
  Eintrag wieder auf die volle Woche (Montag–Freitag) ausgedehnt.
– Die Stundenberechnung (Liefertermin/Restaufwand) berücksichtigt diese
  Aufteilung: pro Eintrag werden nur die Arbeitsstunden des tatsächlichen
  Datumsbereichs (start_date–end_date) angerechnet, nicht die volle KW.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Set
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
    variant_id:    Optional[int] = None  # wenn None → aktive Variante wird verwendet

class PlanningDelete(BaseModel):
    planning_id:   Optional[int] = None  # direkte Löschung per PK
    staff:         Optional[str] = None
    calendar_week: Optional[str] = None
    project_id:    Optional[int] = None
    task_id:       Optional[int] = None
    variant_id:    Optional[int] = None  # wenn None → aktive Variante wird verwendet

# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _get_active_variant_id(cur) -> int:
    """Gibt die variant_id der aktiven Planungsvariante zurück."""
    cur.execute("SELECT variant_id FROM planning_variant WHERE is_active = TRUE LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=409,
            detail="Keine aktive Planungsvariante gefunden. Bitte zuerst eine Variante aktivieren.")
    return row["variant_id"]

def _resolve_variant_id(cur, variant_id: Optional[int]) -> int:
    """Gibt variant_id zurück – entweder die übergebene oder die aktive."""
    if variant_id is not None:
        cur.execute("SELECT variant_id FROM planning_variant WHERE variant_id = %s", (variant_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404,
                detail=f"Planungsvariante {variant_id} nicht gefunden.")
        return variant_id
    return _get_active_variant_id(cur)

def _week_bounds(week_key: str):
    """Gibt (Montag, Freitag) der ISO-Kalenderwoche zurück."""
    year, week = int(week_key.split("-W")[0]), int(week_key.split("-W")[1])
    monday = date.fromisocalendar(year, week, 1)
    friday = monday + timedelta(days=4)
    return monday, friday

def _current_week_key() -> str:
    """Gibt den Schlüssel ('YYYY-WNN') der aktuellen Kalenderwoche zurück."""
    iso = date.today().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

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
    """
    True wenn der Mitarbeiter ALLE Arbeitstage der KW abwesend ist.

    Geändert gegenüber ursprünglicher Implementierung:
    – Früher: blockiert bei > 50 % Abwesenheit (≥ 3 von 5 Tagen)
    – Jetzt:  blockiert nur noch bei 100 % Abwesenheit (alle Arbeitstage)
      → Zuweisungen sind auch bei 4 von 5 Abwesenheitstagen möglich.
      Die Stundenberechnung via _effective_hours_in_date_range() berücksichtigt
      die tatsächlichen Anwesenheitstage bereits korrekt.
    """
    total  = _total_workdays_in_week(week_key, at_hols)
    if total == 0:
        return True
    absent = _absent_workdays_in_week(shortname, week_key, absences, at_hols)
    # Nur blockieren wenn wirklich ALLE Arbeitstage abwesend
    return absent >= total

def _effective_hours_in_date_range(shortname: str, hours_per_day: float,
                                   range_start: date, range_end: date,
                                   absences: list, at_hols: Set[date]) -> float:
    """
    Effektive Stunden des Mitarbeiters im angegebenen Datumsbereich (inkl.
    beider Enden): hours_per_day × (Arbeitstage im Bereich − Abwesenheitstage
    im Bereich).

    Wird für Planungseinträge verwendet, die nicht zwingend eine volle
    Kalenderwoche abdecken – z.B. wenn einem Mitarbeiter zwei Projekte in
    derselben KW zugewiesen sind und die Woche dadurch in Mo–Mi / Do–Fr
    aufgeteilt wurde. _effective_hours_in_week() deckt den Sonderfall
    „ganze Woche" ab und ruft intern diese Funktion auf.

    Nachträgliche Abwesenheiten: Da diese Funktion die Abwesenheiten immer
    aus der frisch übergebenen Liste liest (die ihrerseits bei jedem API-
    Aufruf neu aus der DB geladen wird), werden nachträglich eingetragene
    Abwesenheiten für bestehende Planungen automatisch korrekt berücksichtigt –
    die Stunden sinken entsprechend, ohne dass die Planungseinträge selbst
    geändert werden müssen.
    """
    if range_end < range_start:
        return 0.0

    total_days = 0
    cur = range_start
    while cur <= range_end:
        if cur.weekday() < 5 and cur not in at_hols:
            total_days += 1
        cur += timedelta(days=1)

    absent_days: Set[date] = set()
    for ab in absences:
        if ab["shortname"] != shortname:
            continue
        s = max(ab["absence_from"], range_start)
        e = min(ab["absence_to"],   range_end)
        c = s
        while c <= e:
            if c.weekday() < 5 and c not in at_hols:
                absent_days.add(c)
            c += timedelta(days=1)

    return max(0.0, (total_days - len(absent_days)) * float(hours_per_day))

def _effective_hours_in_week(shortname: str, hours_per_day: float,
                             week_key: str, absences: list, at_hols: Set[date]) -> float:
    """
    Effektive Stunden des Mitarbeiters in der vollen KW (Mo–Fr):
    hours_per_day × (Arbeitstage − Abwesenheitstage).
    Delegiert an _effective_hours_in_date_range() mit den Wochengrenzen.
    """
    monday, friday = _week_bounds(week_key)
    return _effective_hours_in_date_range(shortname, hours_per_day, monday, friday, absences, at_hols)

def _build_at_hols(start: date, end: date) -> Set[date]:
    years = set(range(start.year, end.year + 1))
    hols: Set[date] = set()
    for y in years:
        hols |= get_austrian_holidays(y)
    return hols

def _next_week_key(base_date: date) -> str:
    next_week_date = base_date + timedelta(weeks=1)
    nxt_iso = next_week_date.isocalendar()
    return f"{nxt_iso[0]}-W{nxt_iso[1]:02d}"

# ── GET / ──────────────────────────────────────────────────────────────────────

@router.get("/")
def get_planning(
    use_current_week: bool = False,
    end_week: Optional[str] = None,          # Format: 'YYYY-WNN'
    variant_id: Optional[int] = None,        # wenn None → aktive Variante
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

    variant_id: optional; wenn nicht angegeben, wird die aktive Variante verwendet.
    """
    with get_cursor() as cur:

        # ── Variante auflösen ────────────────────────────────────────────────
        resolved_variant_id = _resolve_variant_id(cur, variant_id)

        cur.execute("SELECT variant_id, variant_name, is_active, created_at FROM planning_variant WHERE variant_id = %s",
                    (resolved_variant_id,))
        variant_info = dict(cur.fetchone())

        # Initialisiere Sets für effektive IDs
        effective_project_ids_set: Set[int] = set()
        effective_task_ids_set: Set[int] = set()
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

        if (filter_project_ids or filter_project_names or filter_task_ids or filter_task_names) and \
           (not effective_project_ids and not effective_task_ids):
            raise HTTPException(404, "Die angegebenen Projekte/Tasks konnten nicht gefunden werden oder existieren nicht.")

        # Ermittle zugehörige Projekt-IDs von gefilterten Tasks
        task_project_ids_set: Set[int] = set()
        if effective_task_ids:
            cur.execute("SELECT DISTINCT project_id FROM tasks WHERE task_id = ANY(%s) AND project_id IS NOT NULL", (effective_task_ids,))
            for row in cur.fetchall():
                task_project_ids_set.add(row["project_id"])

        all_relevant_project_ids = list(effective_project_ids_set.union(task_project_ids_set))

        # ── Filter-Flag: wurde mit Projekt- oder Task-Filter aufgerufen? ─────
        has_any_filter = bool(effective_project_ids_set or effective_task_ids_set)
        filtered_status_project_ids: Set[int] = effective_project_ids_set.union(task_project_ids_set)

        # ── Zeitbereich aus Projektdaten ─────────────────────────────────────
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

        if not use_current_week:
            db_start = range_row["min_start"] if (range_row and range_row["min_start"]) else None
            cur.execute("SELECT MIN(start_date) AS min_plan FROM planning WHERE variant_id = %s", (resolved_variant_id,))
            plan_row = cur.fetchone()
            if plan_row and plan_row["min_plan"]:
                if not db_start or plan_row["min_plan"] < db_start:
                    db_start = plan_row["min_plan"]

            if db_start:
                ds_iso = db_start.isocalendar()
                start_date = date.fromisocalendar(ds_iso[0], ds_iso[1], 1)
            else:
                start_date = cur_monday
        else:
            start_date = cur_monday

        max_due = range_row["max_due"] if (range_row and range_row["max_due"]) else None

        if all_relevant_project_ids or effective_task_ids:
            plan_end_sql = """
                SELECT MAX(pl.end_date) AS max_plan_end
                FROM planning pl
                LEFT JOIN tasks t ON t.task_id = pl.task_id
                WHERE pl.variant_id = %s
            """
            plan_end_params: list = [resolved_variant_id]
            plan_end_parts: list = []
            if effective_task_ids:
                plan_end_parts.append("pl.task_id = ANY(%s)")
                plan_end_params.append(effective_task_ids)
            if effective_project_ids:
                plan_end_parts.append("(pl.project_id = ANY(%s) OR t.project_id = ANY(%s))")
                plan_end_params.extend([effective_project_ids, effective_project_ids])
            if plan_end_parts:
                plan_end_sql += " AND (" + " OR ".join(plan_end_parts) + ")"
            cur.execute(plan_end_sql, plan_end_params)
            plan_end_row = cur.fetchone()
            max_plan_end = plan_end_row["max_plan_end"] if plan_end_row else None
            if max_plan_end:
                max_due = max(max_due, max_plan_end) if max_due else max_plan_end

        if max_due:
            md_iso   = max_due.isocalendar()
            end_date = date.fromisocalendar(md_iso[0], md_iso[1], 7) + timedelta(weeks=1)
        else:
            end_date = start_date + timedelta(weeks=12)

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

        # ── Bestehende Planungen laden (gefiltert nach variant_id) ──────────
        sql_plannings = """
            SELECT pl.planning_id, pl.task_id, pl.project_id, pl.staff, pl.role_id,
                   pl.start_date, pl.end_date, pl.variant_id,
                   p.project_name, p.color_hexcode AS project_color,
                   t.task_name,    t.color_hexcode AS task_color
            FROM planning pl
            LEFT JOIN project p ON p.project_id = pl.project_id
            LEFT JOIN tasks   t ON t.task_id    = pl.task_id
            WHERE pl.end_date   >= %s
            AND   pl.start_date <= %s
            AND   pl.variant_id = %s
        """
        params_plannings = [start_date, end_date, resolved_variant_id]

        combined_planning_filter_parts = []
        combined_planning_params = []

        if effective_task_ids:
            combined_planning_filter_parts.append("pl.task_id = ANY(%s)")
            combined_planning_params.append(effective_task_ids)

        if effective_project_ids:
            combined_planning_filter_parts.append("(pl.project_id = ANY(%s) OR t.project_id = ANY(%s))")
            combined_planning_params.extend([effective_project_ids, effective_project_ids])

        if combined_planning_filter_parts:
            sql_plannings += " AND (" + " OR ".join(combined_planning_filter_parts) + ")"
            params_plannings.extend(combined_planning_params)

        cur.execute(sql_plannings, params_plannings)
        plannings_raw = [dict(r) for r in cur.fetchall()]

        if not plannings_raw and (effective_project_ids or effective_task_ids):
            raise HTTPException(404, "Für die angegebenen Projekte/Tasks wurde keine Planung im sichtbaren Zeitraum gefunden.")

        # ── Verfügbare Projekte ──────────────────────────────────────────────
        sql_all_projects = """
            SELECT p.project_id, p.project_name, p.customer, p.color_hexcode,
                   p.start_date, p.due_date, p.target_hours, p.impl_hours, p.test_hours,
                   p.project_type
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
        all_projects_filtered = cur.fetchall()

        # Letztes worked_hours-Datum je Projekt
        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

        # ── Verfügbare Tasks ─────────────────────────────────────────────────
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
        if all_relevant_project_ids:
            where_clauses_tasks.append("t.project_id = ANY(%s)")
            params_available_tasks.append(all_relevant_project_ids)

        if where_clauses_tasks:
            sql_available_tasks += " WHERE " + " OR ".join(where_clauses_tasks)

        sql_available_tasks += " ORDER BY t.task_name"
        cur.execute(sql_available_tasks, params_available_tasks)
        available_tasks = cur.fetchall()

        # ── Feiertage ────────────────────────────────────────────────────────
        at_hols = _build_at_hols(start_date, end_date)

        # ── Kalenderwochen-Liste ─────────────────────────────────────────────
        weeks: List[str] = []
        cur_d = start_date
        while cur_d <= end_date:
            wk = iso_week_key(cur_d)
            if not weeks or weeks[-1] != wk:
                weeks.append(wk)
            cur_d += timedelta(days=7)

        # ── Abwesenheits-Map ─────────────────────────────────────────────────
        # Felder je KW:
        #   absent_days   – Anzahl abwesender Arbeitstage
        #   total_days    – Gesamtzahl Arbeitstage in der KW
        #   is_majority   – True wenn ALLE Arbeitstage abwesend (= Zuweisung gesperrt)
        #   is_partial    – True wenn ≥ 3 Tage abwesend aber nicht alle
        #                   (= Zuweisung erlaubt, aber optisch hervorgehoben)
        #   type          – Abwesenheitstyp (für Tooltip)
        absence_map: dict = {}
        for sr in staff_roles:
            name = sr["shortname"]
            absence_map[name] = {}
            for wk in weeks:
                absent_days = _absent_workdays_in_week(name, wk, absences_list, at_hols)
                total_days  = _total_workdays_in_week(wk, at_hols)
                if absent_days == 0:
                    continue

                monday, friday = _week_bounds(wk)
                ab_type = "Abwesend"
                for ab in absences_list:
                    if (ab["shortname"] == name
                            and ab["absence_from"] <= friday
                            and ab["absence_to"]   >= monday):
                        ab_type = ab["absence_type"]
                        break

                is_majority = absent_days >= total_days  # blockiert nur noch bei 100 %
                is_partial  = (not is_majority) and (absent_days >= 3)  # ≥ 3 Tage → optische Warnung

                absence_map[name][wk] = {
                    "absent_days": absent_days,
                    "total_days":  total_days,
                    "is_majority": is_majority,
                    "is_partial":  is_partial,
                    "type":        ab_type,
                }

        # ── Kapazität pro Mitarbeiter + KW ───────────────────────────────────
        capacity_by_staff: dict = {}
        capacity_totals:   dict = {wk: 0.0 for wk in weeks}

        staff_hpd_map = {r["shortname"]: float(r["hours_per_day"]) for r in staff_roles}

        for name, hpd in staff_hpd_map.items():
            week_hours: dict = {}
            for wk in weeks:
                h = _effective_hours_in_week(name, hpd, wk, absences_list, at_hols)
                week_hours[wk] = h
                capacity_totals[wk] += h
            capacity_by_staff[name] = {
                "hours_per_day": hpd,
                "week_hours":    week_hours,
            }

        # ── end_week: Enddatum begrenzen ─────────────────────────────────────
        if end_week:
            weeks = [wk for wk in weeks if wk <= end_week]

        # ── Outdated-Flag in plan_map ─────────────────────────────────────────
        plan_map: dict = {}
        for pl in plannings_raw:
            wk    = iso_week_key(pl["start_date"])
            pid   = pl["project_id"]
            entry = dict(pl)

            entry["is_outdated"] = (
                pid is not None
                and pid in max_worked_day
                and max_worked_day[pid] > pl["end_date"]
            )
            plan_map.setdefault(pl["staff"], {}).setdefault(wk, []).append(entry)

        # ── Ist-Liefertermin je KW (für die Liefertermin-Zeile) ──────────────
        cur.execute("""
            SELECT pl.project_id,
                   pl.staff, pl.start_date, pl.end_date,
                   r.role, s.hours_per_day
            FROM planning pl
            JOIN roles r ON r.role_id   = pl.role_id
            JOIN staff s ON s.shortname = pl.staff
            WHERE pl.project_id IS NOT NULL
            AND   pl.variant_id = %s
        """, (resolved_variant_id,))
        plan_rows_status = [dict(r) for r in cur.fetchall()]

        if plan_rows_status:
            pstatus_staff = list({r["staff"] for r in plan_rows_status})
            pstatus_min_d = min(r["start_date"] for r in plan_rows_status)
            pstatus_max_d = max(r["end_date"]   for r in plan_rows_status)
            cur.execute("""
                SELECT shortname, absence_from, absence_to
                FROM absence
                WHERE shortname = ANY(%s)
                AND absence_to >= %s AND absence_from <= %s
            """, (pstatus_staff, pstatus_min_d, pstatus_max_d))
            absences_status = [dict(a) for a in cur.fetchall()]
        else:
            absences_status = []

        cur.execute("""
            SELECT project_id,
                   COALESCE(SUM(impl_hours),0) AS worked_impl,
                   COALESCE(SUM(test_hours),0) AS worked_test
            FROM worked_hours GROUP BY project_id
        """)
        worked_map = {r["project_id"]: r for r in cur.fetchall()}

        status_project_sql = """
            SELECT p.project_id, p.project_name, p.color_hexcode,
                   p.impl_hours AS plan_impl, p.test_hours AS plan_test,
                   p.due_date
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE
            AND p.project_type = 'Project'
        """
        status_project_params = []
        if has_any_filter:
            status_project_sql += " AND p.project_id = ANY(%s)"
            status_project_params.append(list(filtered_status_project_ids))

        cur.execute(status_project_sql, status_project_params)
        projects_for_status = {r["project_id"]: dict(r) for r in cur.fetchall()}

    # ── Ist-Liefertermin berechnen (außerhalb des Cursor-Blocks) ─────────────
    at_hols_status = _build_at_hols(
        min((r["start_date"] for r in plan_rows_status), default=date.today()),
        max((r["end_date"]   for r in plan_rows_status), default=date.today()) + timedelta(weeks=12)
    ) if plan_rows_status else set()

    plan_agg_status: dict = {}
    last_end_map_status: dict = {}

    for pr in plan_rows_status:
        pid = pr["project_id"]

        if has_any_filter and pid not in filtered_status_project_ids:
            continue

        last_worked_day = max_worked_day.get(pid)
        if last_worked_day is not None and pr["end_date"] <= last_worked_day:
            continue

        h = _effective_hours_in_date_range(
            pr["staff"], float(pr["hours_per_day"]),
            pr["start_date"], pr["end_date"],
            absences_status, at_hols_status)

        plan_agg_status.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
        role_key = pr["role"] if pr["role"] in ("Developer", "Tester") else "Developer"
        plan_agg_status[pid][role_key] += h

        prev = last_end_map_status.get(pid)
        if prev is None or pr["end_date"] > prev:
            last_end_map_status[pid] = pr["end_date"]

    ist_kw_map: dict = {}

    for pid, proj in projects_for_status.items():
        w  = worked_map.get(pid, {"worked_impl": 0, "worked_test": 0})
        pa = plan_agg_status.get(pid, {"Developer": 0.0, "Tester": 0.0})

        remaining_impl = proj["plan_impl"] - float(w["worked_impl"]) - pa["Developer"]
        remaining_test = proj["plan_test"] - float(w["worked_test"]) - pa["Tester"]
        diff = remaining_impl + remaining_test

        if diff <= 0 or (0 < diff < 15):
            base_date = last_end_map_status.get(pid, date.today())
            ist_kw = _next_week_key(base_date)
            if ist_kw not in ist_kw_map:
                ist_kw_map[ist_kw] = []
            ist_kw_map[ist_kw].append({
                "project_id":    pid,
                "project_name":  proj["project_name"],
                "color_hexcode": proj["color_hexcode"],
            })

    return {
        "weeks":              weeks,
        "staff_roles":        [dict(r) for r in staff_roles],
        "capacity_totals":    capacity_totals,
        "capacity_by_staff":  capacity_by_staff,
        "plannings":          plan_map,
        "absence_map":        absence_map,
        "available_projects": [dict(p) for p in all_projects_filtered],
        "available_tasks":    [dict(t) for t in available_tasks],
        "filter_title_parts": filter_title_parts,
        "variant_info":       variant_info,
        "resolved_variant_id": resolved_variant_id,
        "ist_kw_map":         ist_kw_map,
    }

# ── POST /assign ───────────────────────────────────────────────────────────────

@router.post("/assign")
def assign_planning(data: PlanningEntry):
    """
    Projekt oder Task einem Mitarbeiter für eine KW zuweisen.

    Pro Mitarbeiter/Rolle/Kalenderwoche sind maximal zwei Zuweisungen möglich:
    – 1. Zuweisung: belegt die gesamte Woche (Montag–Freitag).
    – 2. Zuweisung: die bestehende Zuweisung wird auf Montag–Mittwoch
         verkürzt, die neue Zuweisung erhält Donnerstag–Freitag.
    – Eine 3. Zuweisung in derselben Woche wird abgelehnt.

    Abwesenheits-Prüfung:
    – Gesperrt nur noch wenn der Mitarbeiter ALLE Arbeitstage der KW abwesend ist.
    – Bei 1–4 Abwesenheitstagen ist die Zuweisung erlaubt; die effektiven
      Stunden werden via _effective_hours_in_date_range() korrekt reduziert.

    variant_id: optional; wenn None → aktive Variante wird verwendet.
    """
    if data.task_id is None and data.project_id is None:
        raise HTTPException(422, "task_id oder project_id muss angegeben sein")

    monday, friday = _week_bounds(data.calendar_week)
    wednesday = monday + timedelta(days=2)
    thursday  = monday + timedelta(days=3)

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
            "Mitarbeiter ist in dieser Woche vollständig abwesend – Zuweisung nicht möglich")

    with get_cursor(commit=True) as cur:
        resolved_variant_id = _resolve_variant_id(cur, data.variant_id)

        cur.execute("""
            SELECT planning_id, role_id, project_id, task_id, start_date, end_date
            FROM planning
            WHERE staff = %s AND variant_id = %s
            AND start_date <= %s AND end_date >= %s
            ORDER BY start_date ASC
        """, (data.staff, resolved_variant_id, friday, monday))
        existing_rows = cur.fetchall()

        other_role_rows = [r for r in existing_rows if r["role_id"] != data.role_id]
        if other_role_rows:
            raise HTTPException(409,
                "Mitarbeiter ist in dieser Woche bereits als andere Rolle eingeplant")

        same_role_rows = [r for r in existing_rows if r["role_id"] == data.role_id]

        if len(same_role_rows) >= 2:
            raise HTTPException(409,
                "Für diese Woche sind bereits zwei Projekte zugewiesen – mehr ist nicht möglich")

        if len(same_role_rows) == 1:
            existing = same_role_rows[0]
            cur.execute("""
                UPDATE planning SET start_date = %s, end_date = %s
                WHERE planning_id = %s
            """, (monday, wednesday, existing["planning_id"]))

            cur.execute("""
                INSERT INTO planning (task_id, project_id, staff, role_id, variant_id, start_date, end_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING planning_id
            """, (data.task_id, data.project_id, data.staff,
                  data.role_id, resolved_variant_id, thursday, friday))
            new_id = cur.fetchone()["planning_id"]

            return {"status": "ok", "planning_id": new_id,
                    "start_date": str(thursday), "end_date": str(friday)}

        cur.execute("""
            INSERT INTO planning (task_id, project_id, staff, role_id, variant_id, start_date, end_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING planning_id
        """, (data.task_id, data.project_id, data.staff,
              data.role_id, resolved_variant_id, monday, friday))
        new_id = cur.fetchone()["planning_id"]

    return {"status": "ok", "planning_id": new_id,
            "start_date": str(monday), "end_date": str(friday)}

# ── DELETE /remove ─────────────────────────────────────────────────────────────

@router.delete("/remove")
def remove_planning(data: PlanningDelete):
    """
    Planungszuordnung entfernen.
    Bevorzugt: direkte Löschung per planning_id.
    Fallback: Löschung per staff + calendar_week (+ optionale project_id/task_id).

    Wurde die Woche zuvor auf zwei Zuweisungen aufgeteilt (Mo–Mi / Do–Fr),
    wird die verbleibende Zuweisung nach dem Löschen wieder auf die volle
    Woche (Mo–Fr) ausgedehnt.
    """
    with get_cursor(commit=True) as cur:

        deleted_row = None

        if data.planning_id is not None:
            cur.execute("""
                SELECT planning_id, staff, role_id, variant_id, start_date, end_date
                FROM planning WHERE planning_id = %s
            """, (data.planning_id,))
            deleted_row = cur.fetchone()
            cur.execute("DELETE FROM planning WHERE planning_id = %s", (data.planning_id,))

        else:
            if not data.staff or not data.calendar_week:
                raise HTTPException(422, "planning_id oder (staff + calendar_week) müssen angegeben sein")

            monday, friday = _week_bounds(data.calendar_week)
            resolved_variant_id = _resolve_variant_id(cur, data.variant_id)

            sql = """
                SELECT planning_id, staff, role_id, variant_id, start_date, end_date
                FROM planning
                WHERE staff = %s AND variant_id = %s
                AND start_date <= %s AND end_date >= %s
            """
            params: list = [data.staff, resolved_variant_id, friday, monday]

            if data.project_id is not None:
                sql += " AND project_id = %s"
                params.append(data.project_id)
            if data.task_id is not None:
                sql += " AND task_id = %s"
                params.append(data.task_id)

            sql += " ORDER BY start_date ASC LIMIT 1"
            cur.execute(sql, params)
            deleted_row = cur.fetchone()
            if deleted_row:
                cur.execute("DELETE FROM planning WHERE planning_id = %s", (deleted_row["planning_id"],))

        if deleted_row:
            ds_iso      = deleted_row["start_date"].isocalendar()
            week_monday = date.fromisocalendar(ds_iso[0], ds_iso[1], 1)
            week_friday = week_monday + timedelta(days=4)

            cur.execute("""
                SELECT planning_id FROM planning
                WHERE staff = %s AND role_id = %s AND variant_id = %s
                AND start_date <= %s AND end_date >= %s
            """, (deleted_row["staff"], deleted_row["role_id"], deleted_row["variant_id"],
                  week_friday, week_monday))
            remaining = cur.fetchall()

            if len(remaining) == 1:
                cur.execute("""
                    UPDATE planning SET start_date = %s, end_date = %s
                    WHERE planning_id = %s
                """, (week_monday, week_friday, remaining[0]["planning_id"]))

    return {"status": "ok"}

# ── GET /project_status ────────────────────────────────────────────────────────

@router.get("/project_status")
def project_planning_status(variant_id: Optional[int] = None):
    """
    Planungsstatus je aktivem Projekt (variant_id: optional, default = aktive Variante).
    """
    with get_cursor() as cur:

        resolved_variant_id = _resolve_variant_id(cur, variant_id)

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

        cur.execute("""
            SELECT project_id,
                   COALESCE(SUM(impl_hours),0) AS worked_impl,
                   COALESCE(SUM(test_hours),0) AS worked_test
            FROM worked_hours GROUP BY project_id
        """)
        worked = {r["project_id"]: r for r in cur.fetchall()}

        cur.execute("""
            SELECT pl.project_id, pl.task_id,
                   pl.staff, pl.start_date, pl.end_date,
                   r.role, s.hours_per_day
            FROM planning pl
            JOIN roles r ON r.role_id   = pl.role_id
            JOIN staff s ON s.shortname = pl.staff
            WHERE pl.project_id IS NOT NULL
            AND   pl.variant_id = %s
        """, (resolved_variant_id,))
        plan_rows = [dict(r) for r in cur.fetchall()]

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

        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day_status = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

    at_hols = _build_at_hols(min_d, max_d + timedelta(weeks=12))

    today_iso         = date.today().isocalendar()
    current_week_key  = f"{today_iso[0]}-W{today_iso[1]:02d}"

    plan_cur:          dict = {}
    plan_calendar_prev: dict = {}
    plan_calendar_cur:  dict = {}
    last_end_map:      dict = {}

    for pr in plan_rows:
        pid = pr["project_id"]
        wk  = iso_week_key(pr["start_date"])

        h = _effective_hours_in_date_range(
            pr["staff"], float(pr["hours_per_day"]),
            pr["start_date"], pr["end_date"],
            all_absences, at_hols)

        role_key = pr["role"] if pr["role"] in ("Developer", "Tester") else "Developer"

        if wk < current_week_key:
            plan_calendar_prev.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
            plan_calendar_prev[pid][role_key] += h
        else:
            plan_calendar_cur.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
            plan_calendar_cur[pid][role_key] += h

        is_outdated = (
            pid in max_worked_day_status
            and max_worked_day_status[pid] > pr["end_date"]
        )
        if not is_outdated:
            plan_cur.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
            plan_cur[pid][role_key] += h

            prev_end_date = last_end_map.get(pid)
            if prev_end_date is None or pr["end_date"] > prev_end_date:
                last_end_map[pid] = pr["end_date"]

    result = []
    for p in projects:
        pid = p["project_id"]
        w   = worked.get(pid, {"worked_impl": 0, "worked_test": 0})
        pc  = plan_cur.get(pid, {"Developer": 0.0, "Tester": 0.0})
        cwp = plan_calendar_prev.get(pid, {"Developer": 0.0, "Tester": 0.0})
        cwc = plan_calendar_cur.get(pid, {"Developer": 0.0, "Tester": 0.0})

        done_impl = float(w["worked_impl"])
        done_test = float(w["worked_test"])

        remaining_impl = p["plan_impl"] - done_impl - pc["Developer"]
        remaining_test = p["plan_test"] - done_test - pc["Tester"]
        diff           = remaining_impl + remaining_test

        restaufwand = (
            p["target_hours"]
            - float(w["worked_impl"])
            - float(w["worked_test"])
        )

        status_color      = ""
        ist_kw_calculated = None

        if diff <= 0 or (diff > 0 and diff < 15):
            status_color = "lightgreen"
            base_date_for_next_week = last_end_map.get(pid, date.today())
            ist_kw_calculated = _next_week_key(base_date_for_next_week)
        else:
            status_color      = "red"
            ist_kw_calculated = None

        due_kw = None
        if p["due_date"]:
            iso    = p["due_date"].isocalendar()
            due_kw = f"{iso[0]}-W{iso[1]:02d}"

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
            "soll_impl":         p["plan_impl"],
            "soll_test":         p["plan_test"],
            "done_impl":         done_impl,
            "done_test":         done_test,
            "planned_prev_impl": cwp["Developer"],
            "planned_prev_test": cwp["Tester"],
            "planned_cur_impl":  cwc["Developer"],
            "planned_cur_test":  cwc["Tester"],
        })

    return result

# ── GET /variants ──────────────────────────────────────────────────────────────

@router.get("/variants")
def list_variants():
    """Alle Planungsvarianten auflisten."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT variant_id, variant_name, is_active, created_at, active_since
            FROM planning_variant
            ORDER BY created_at DESC
        """)
        return cur.fetchall()
