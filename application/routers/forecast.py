"""
routers/forecast.py – Endpunkte für den Liefertermin-Forecast
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import date, timedelta

import holidays # Wird benötigt, da _build_at_hols es verwendet
from db import get_cursor # Datenbank-Zugriff
# Annahme: iso_week_key und get_austrian_holidays kommen aus capacity
from capacity import iso_week_key, get_austrian_holidays 

router = APIRouter()

# ── Hilfsfunktionen (dupliziert aus planning.py zur Selbstständigkeit) ────
def _week_bounds(week_key: str):
    """Gibt (Montag, Freitag) der ISO-Kalenderwoche zurück."""
    year, week = int(week_key.split("-W")[0]), int(week_key.split("-W")[1])
    monday = date.fromisocalendar(year, week, 1)
    friday = monday + timedelta(days=4)
    return monday, friday

def _absent_workdays_in_week(shortname: str, week_key: str,
                              absences: list, at_hols: set) -> int:
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

def _total_workdays_in_week(week_key: str, at_hols: set) -> int:
    """Gesamtzahl Arbeitstage (Mo–Fr, kein Feiertag) in der KW."""
    monday, friday = _week_bounds(week_key)
    count = 0
    cur = monday
    while cur <= friday:
        if cur.weekday() < 5 and cur not in at_hols:
            count += 1
        cur += timedelta(days=1)
    return count

def _effective_hours_in_week(shortname: str, hours_per_day: float,
                              week_key: str, absences: list, at_hols: set) -> float:
    """
    Effektive Stunden des Mitarbeiters in der KW:
    hours_per_day × (Arbeitstage − Abwesenheitstage)
    """
    total  = _total_workdays_in_week(week_key, at_hols)
    absent = _absent_workdays_in_week(shortname, week_key, absences, at_hols)
    return max(0.0, (total - absent) * float(hours_per_day))

def _build_at_hols(start: date, end: date) -> set:
    """Baut eine Menge österreichischer Feiertage für einen Datumsbereich auf."""
    # `get_austrian_holidays` wird aus capacity importiert
    years = set(range(start.year, end.year + 1))
    hols: set = set()
    for y in years:
        hols |= get_austrian_holidays(y) # Annahme, dass diese Funktion existiert
    return hols

# ── Pydantic-Modelle für die Rückgabe ──────────────────────────────────────────
class ForecastProject(BaseModel):
    project_id: int
    project_name: str
    color_hexcode: Optional[str] = None
    due_date: Optional[date] = None # Original due_date

class BurndownPoint(BaseModel):
    week_key: str
    remaining_total: float

class ForecastResponse(BaseModel):
    projects: List[ForecastProject]
    weeks: List[str] # Alle prognostizierten Kalenderwochen
    burndown_data: Dict[str, List[BurndownPoint]] # key: project_id (str), value: Burndown-Punkte

# ── Forecast-Endpunkt ──────────────────────────────────────────────────────────
@router.get("/data", response_model=ForecastResponse)
def get_forecast_data(
    num_projects: int = Query(5, ge=1, description="Anzahl der Projekte für den Forecast"),
    forecast_weeks_duration: int = Query(52, ge=1, description="Dauer des Forecasts in Wochen")
):
    """
    Berechnet die Burndown-Daten für die n wichtigsten Projekte
    basierend auf einer Vorwärtsrechnung der Kapazität.
    """
    with get_cursor() as cur:
        today = date.today()
        current_week_key = iso_week_key(today) # Annahme: iso_week_key aus capacity.py

        # 1. Projekte abrufen und filtern
        #   - project_type = 'Project', planned=TRUE, done=FALSE
        #   - sortiert nach due_date ASC
        #   - limitiert auf num_projects
        cur.execute("""
            SELECT p.project_id, p.project_name, p.color_hexcode,
                   p.impl_hours AS plan_impl, p.test_hours AS plan_test,
                   p.due_date
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE AND p.project_type = 'Project'
            ORDER BY p.due_date ASC NULLS LAST, p.project_name ASC
            LIMIT %s
        """, (num_projects,))
        raw_projects = cur.fetchall()

        if not raw_projects:
            return ForecastResponse(projects=[], weeks=[], burndown_data={})

        projects_data = []
        project_ids = [p["project_id"] for p in raw_projects]
        initial_remaining_impl = {}
        initial_remaining_test = {}

        # 2. Aktuellen Restaufwand ermitteln (geplant - geleistet)
        cur.execute(f"""
            SELECT project_id,
                   COALESCE(SUM(impl_hours),0) AS worked_impl,
                   COALESCE(SUM(test_hours),0) AS worked_test
            FROM worked_hours
            WHERE project_id = ANY(%s)
            GROUP BY project_id
        """, (project_ids,))
        worked_hours_map = {r["project_id"]: r for r in cur.fetchall()}

        for p_row in raw_projects:
            pid = p_row["project_id"]
            worked = worked_hours_map.get(pid, {"worked_impl": 0, "worked_test": 0})
            
            # Restaufwand berechnen, kann nicht negativ sein
            rem_impl = max(0.0, float(p_row["plan_impl"]) - float(worked["worked_impl"]))
            rem_test = max(0.0, float(p_row["plan_test"]) - float(worked["worked_test"]))

            if rem_impl + rem_test > 0: # Nur Projekte mit Restaufwand berücksichtigen
                projects_data.append(ForecastProject(
                    project_id=pid,
                    project_name=p_row["project_name"],
                    color_hexcode=p_row["color_hexcode"],
                    due_date=p_row["due_date"]
                ))
                initial_remaining_impl[pid] = rem_impl
                initial_remaining_test[pid] = rem_test
        
        if not projects_data:
            return ForecastResponse(projects=[], weeks=[], burndown_data={})
            
        # Wenn Projekte mit Restaufwand weniger als num_projects, Liste kürzen
        project_ids_for_forecast = [p.project_id for p in projects_data]


        # 3. Gesamt-Kapazität (Developer und Tester) ermitteln
        # Prognosezeitraum: start_date = heute, end_date = heute + forecast_weeks_duration
        forecast_start_date = today
        forecast_end_date = today + timedelta(weeks=forecast_weeks_duration)

        cur.execute("""
            SELECT s.shortname, s.hours_per_day, s.is_active, r.role
            FROM staff s
            JOIN roles r ON r.shortname = s.shortname
            WHERE r.role IN ('Developer', 'Tester') AND s.is_active = TRUE
        """)
        staff_roles = cur.fetchall()
        
        all_shortnames = list({r["shortname"] for r in staff_roles})

        cur.execute(f"""
            SELECT shortname, absence_from, absence_to, absence_type
            FROM absence
            WHERE shortname = ANY(%s)
            AND absence_to >= %s AND absence_from <= %s
        """, (all_shortnames, forecast_start_date, forecast_end_date))
        absences = [dict(a) for a in cur.fetchall()]

        at_hols = _build_at_hols(forecast_start_date, forecast_end_date)

        # Wöchentliche Kapazitäten pro Rolle
        weeks_list = []
        current_date_for_week = today
        for _ in range(forecast_weeks_duration):
            wk = iso_week_key(current_date_for_week)
            if wk not in weeks_list:
                weeks_list.append(wk)
            current_date_for_week += timedelta(weeks=1)
        
        weekly_dev_capacity: Dict[str, float] = {wk: 0.0 for wk in weeks_list}
        weekly_test_capacity: Dict[str, float] = {wk: 0.0 for wk in weeks_list}

        for sr in staff_roles:
            role = sr["role"]
            shortname = sr["shortname"]
            hpd = float(sr["hours_per_day"])
            for wk in weeks_list:
                effective_h = _effective_hours_in_week(shortname, hpd, wk, absences, at_hols)
                if role == 'Developer':
                    weekly_dev_capacity[wk] += effective_h
                elif role == 'Tester':
                    weekly_test_capacity[wk] += effective_h

        # 4. Vorwärtsrechnung (Simulation) - Burndown-Logik
        
        # Max Kapazität pro Projekt pro Woche (angenommen 8h/Tag)
        MAX_DEV_HOURS_PER_PROJECT_PER_WEEK = 3 * 5 * 8.0 # 3 Developer * 5 Tage * 8h/Tag
        MAX_TEST_HOURS_PER_PROJECT_PER_WEEK = 1 * 5 * 8.0 # 1 Tester * 5 Tage * 8h/Tag

        # Aktueller Stand der verbleibenden Stunden pro Projekt
        current_remaining_impl = {pid: initial_remaining_impl[pid] for pid in project_ids_for_forecast}
        current_remaining_test = {pid: initial_remaining_test[pid] for pid in project_ids_for_forecast}
        
        burndown_data: Dict[str, List[BurndownPoint]] = {str(p.project_id): [] for p in projects_data}

        # Initialer Stand für KW vor dem Forecast
        prev_week_date = today - timedelta(days=7)
        prev_week_key = iso_week_key(prev_week_date)
        for proj in projects_data:
            pid_str = str(proj.project_id)
            burndown_data[pid_str].append(BurndownPoint(
                week_key=prev_week_key,
                remaining_total=initial_remaining_impl.get(proj.project_id, 0.0) + initial_remaining_test.get(proj.project_id, 0.0)
            ))

        # Simulation starten
        for week_key in weeks_list:
            available_dev_for_week = weekly_dev_capacity.get(week_key, 0.0)
            available_test_for_week = weekly_test_capacity.get(week_key, 0.0)

            # Projekte nach Liefertermin priorisieren
            # projects_data ist bereits sortiert, also diese Reihenfolge verwenden
            for proj in projects_data:
                pid = proj.project_id
                
                # Wenn Projekt bereits fertig, überspringen
                if current_remaining_impl.get(pid, 0.0) + current_remaining_test.get(pid, 0.0) <= 0:
                    continue

                # Benötigter Aufwand für diese Woche (maximal pro Projekt)
                needed_dev = min(current_remaining_impl.get(pid, 0.0), MAX_DEV_HOURS_PER_PROJECT_PER_WEEK)
                needed_test = min(current_remaining_test.get(pid, 0.0), MAX_TEST_HOURS_PER_PROJECT_PER_WEEK)

                # Tatsächlich zugeteilter Aufwand, basierend auf globaler Kapazität
                allocated_dev = min(needed_dev, available_dev_for_week)
                allocated_test = min(needed_test, available_test_for_week)

                # Kapazität abziehen
                available_dev_for_week -= allocated_dev
                available_test_for_week -= allocated_test
                
                # Restliche Stunden des Projekts aktualisieren
                current_remaining_impl[pid] = max(0.0, current_remaining_impl.get(pid, 0.0) - allocated_dev)
                current_remaining_test[pid] = max(0.0, current_remaining_test.get(pid, 0.0) - allocated_test)

            # Burndown-Punkte für die aktuelle Woche speichern
            for proj in projects_data:
                pid_str = str(proj.project_id)
                remaining_total = current_remaining_impl.get(proj.project_id, 0.0) + current_remaining_test.get(proj.project_id, 0.0)
                burndown_data[pid_str].append(BurndownPoint(week_key=week_key, remaining_total=remaining_total))
            
            # Prüfen, ob alle Projekte fertig sind
            all_finished = True
            for proj in projects_data:
                if current_remaining_impl.get(proj.project_id, 0.0) + current_remaining_test.get(proj.project_id, 0.0) > 0:
                    all_finished = False
                    break
            if all_finished:
                # Füge für die restlichen Wochen bis zum forecast_weeks_duration 0-Werte hinzu,
                # um die Graphen einheitlich bis zum Ende zu ziehen
                remaining_weeks_idx = weeks_list.index(week_key) + 1
                for future_wk in weeks_list[remaining_weeks_idx:]:
                    for p in projects_data:
                        pid_str = str(p.project_id)
                        burndown_data[pid_str].append(BurndownPoint(week_key=future_wk, remaining_total=0.0))
                break # Simulation beenden, da alle Projekte abgearbeitet sind

    return ForecastResponse(
        projects=projects_data,
        weeks=weeks_list,
        burndown_data=burndown_data
    )
