"""
routers/forecast.py – Endpunkte für den Liefertermin-Forecast

Trennung von Berechnung (hier) und Rendering (charts.py).
"""
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import date, timedelta

from db import get_cursor
from capacity import iso_week_key, get_austrian_holidays

router = APIRouter()


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _week_bounds(week_key: str):
    """Gibt (Montag, Freitag) der ISO-Kalenderwoche zurück."""
    year, week = int(week_key.split("-W")[0]), int(week_key.split("-W")[1])
    monday = date.fromisocalendar(year, week, 1)
    friday = monday + timedelta(days=4)
    return monday, friday


def _absent_workdays_in_week(shortname: str, week_key: str,
                              absences: list, at_hols: set) -> int:
    monday, friday = _week_bounds(week_key)
    absent_days = set()
    for ab in absences:
        if ab["shortname"] != shortname:
            continue
        cur = max(ab["absence_from"], monday)
        end = min(ab["absence_to"], friday)
        while cur <= end:
            if cur.weekday() < 5 and cur not in at_hols:
                absent_days.add(cur)
            cur += timedelta(days=1)
    return len(absent_days)


def _total_workdays_in_week(week_key: str, at_hols: set) -> int:
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
    total  = _total_workdays_in_week(week_key, at_hols)
    absent = _absent_workdays_in_week(shortname, week_key, absences, at_hols)
    return max(0.0, (total - absent) * float(hours_per_day))


def _build_at_hols(start: date, end: date) -> set:
    years = set(range(start.year, end.year + 1))
    hols: set = set()
    for y in years:
        hols |= get_austrian_holidays(y)
    return hols


# ── Pydantic-Modelle ───────────────────────────────────────────────────────────

class ForecastProject(BaseModel):
    project_id:    int
    project_name:  str
    color_hexcode: Optional[str] = None
    due_date:      Optional[date] = None


class BurndownPoint(BaseModel):
    week_key:        str
    remaining_total: float


class ForecastResponse(BaseModel):
    projects:      List[ForecastProject]
    weeks:         List[str]                          # Alle Wochen inkl. Vorwoche als Startpunkt
    burndown_data: Dict[str, List[BurndownPoint]]     # key = str(project_id)


# ── Kalkulationslogik ──────────────────────────────────────────────────────────

def calculate_forecast(num_projects: int, forecast_weeks_duration: int) -> ForecastResponse:
    """
    Reine Berechnungsfunktion – kein Rendering.
    Gibt strukturierte Burndown-Daten zurück.
    """
    with get_cursor() as cur:

        # 1. Projekte laden (type='Project', planned, not done), nach due_date sortiert
        cur.execute("""
            SELECT p.project_id, p.project_name, p.color_hexcode,
                   p.impl_hours  AS plan_impl,
                   p.test_hours  AS plan_test,
                   p.due_date
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE AND p.project_type = 'Project'
            ORDER BY p.due_date ASC NULLS LAST, p.project_name ASC
            LIMIT %s
        """, (num_projects,))
        raw_projects = cur.fetchall()

        if not raw_projects:
            return ForecastResponse(projects=[], weeks=[], burndown_data={})

        project_ids = [p["project_id"] for p in raw_projects]

        # 2. Geleistete Stunden je Projekt
        cur.execute("""
            SELECT project_id,
                   COALESCE(SUM(impl_hours), 0) AS worked_impl,
                   COALESCE(SUM(test_hours), 0) AS worked_test
            FROM worked_hours
            WHERE project_id = ANY(%s)
            GROUP BY project_id
        """, (project_ids,))
        worked_map = {r["project_id"]: r for r in cur.fetchall()}

        # 3. Aktiven Staff (Developer + Tester) laden
        cur.execute("""
            SELECT s.shortname, s.hours_per_day, r.role
            FROM staff s
            JOIN roles r ON r.shortname = s.shortname
            WHERE r.role IN ('Developer', 'Tester') AND s.is_active = TRUE
        """)
        staff_rows = cur.fetchall()
        all_shortnames = list({r["shortname"] for r in staff_rows})

        today               = date.today()
        forecast_start      = today
        forecast_end        = today + timedelta(weeks=forecast_weeks_duration + 1)

        # 4. Abwesenheiten für den Prognosezeitraum
        cur.execute("""
            SELECT shortname, absence_from, absence_to, absence_type
            FROM absence
            WHERE shortname = ANY(%s)
              AND absence_to   >= %s
              AND absence_from <= %s
        """, (all_shortnames, forecast_start, forecast_end))
        absences = [dict(a) for a in cur.fetchall()]

    # Ab hier: reine Python-Berechnung, kein Cursor mehr nötig

    at_hols = _build_at_hols(forecast_start, forecast_end)

    # ── Restaufwand pro Projekt ermitteln ──────────────────────────────────────
    projects_data: List[ForecastProject] = []
    initial_remaining_impl: Dict[int, float] = {}
    initial_remaining_test: Dict[int, float] = {}

    for p_row in raw_projects:
        pid    = p_row["project_id"]
        worked = worked_map.get(pid, {"worked_impl": 0, "worked_test": 0})

        rem_impl = max(0.0, float(p_row["plan_impl"]) - float(worked["worked_impl"]))
        rem_test = max(0.0, float(p_row["plan_test"]) - float(worked["worked_test"]))

        # Nur Projekte mit verbleibendem Aufwand berücksichtigen
        if rem_impl + rem_test > 0:
            projects_data.append(ForecastProject(
                project_id=pid,
                project_name=p_row["project_name"],
                color_hexcode=p_row["color_hexcode"],
                due_date=p_row["due_date"],
            ))
            initial_remaining_impl[pid] = rem_impl
            initial_remaining_test[pid] = rem_test

    if not projects_data:
        return ForecastResponse(projects=[], weeks=[], burndown_data={})

    # ── Wochenliste aufbauen ───────────────────────────────────────────────────
    # Vorwoche als visueller Startpunkt (zeigt Ausgangslage vor Forecast)
    prev_week_key = iso_week_key(today - timedelta(days=7))

    forecast_weeks: List[str] = []
    cur_d = today
    seen: set = set()
    for _ in range(forecast_weeks_duration):
        wk = iso_week_key(cur_d)
        if wk not in seen:
            forecast_weeks.append(wk)
            seen.add(wk)
        cur_d += timedelta(weeks=1)

    # Gesamte Wochenliste: Vorwoche + Forecast-Wochen
    # → exakt gleich viele Einträge wie Burndown-Punkte pro Projekt
    full_weeks_list = [prev_week_key] + forecast_weeks

    # ── Wöchentliche Kapazität pro Rolle berechnen ─────────────────────────────
    # Nur für die Forecast-Wochen (nicht die Vorwoche, dort wird nicht gearbeitet)
    weekly_dev_cap:  Dict[str, float] = {wk: 0.0 for wk in forecast_weeks}
    weekly_test_cap: Dict[str, float] = {wk: 0.0 for wk in forecast_weeks}

    for sr in staff_rows:
        role      = sr["role"]
        shortname = sr["shortname"]
        hpd       = float(sr["hours_per_day"])
        for wk in forecast_weeks:
            h = _effective_hours_in_week(shortname, hpd, wk, absences, at_hols)
            if role == "Developer":
                weekly_dev_cap[wk]  += h
            elif role == "Tester":
                weekly_test_cap[wk] += h

    # ── Vorwärtsrechnung (Simulation) ──────────────────────────────────────────
    # Kapazitätsgrenzen pro Projekt pro Woche (max. 3 Dev / 1 Tester)
    MAX_DEV_PER_PROJECT  = 3 * 5 * 8.0   # 3 Entwickler × 5 Tage × 8 h
    MAX_TEST_PER_PROJECT = 1 * 5 * 8.0   # 1 Tester    × 5 Tage × 8 h

    current_impl = {p.project_id: initial_remaining_impl[p.project_id] for p in projects_data}
    current_test = {p.project_id: initial_remaining_test[p.project_id] for p in projects_data}

    burndown_data: Dict[str, List[BurndownPoint]] = {
        str(p.project_id): [] for p in projects_data
    }

    # Initialer Punkt (Vorwoche) = Ausgangslage, noch keine Arbeit abgezogen
    for proj in projects_data:
        pid_str = str(proj.project_id)
        burndown_data[pid_str].append(BurndownPoint(
            week_key=prev_week_key,
            remaining_total=(
                initial_remaining_impl[proj.project_id]
                + initial_remaining_test[proj.project_id]
            ),
        ))

    # Simulation über die eigentlichen Forecast-Wochen
    for wk in forecast_weeks:
        avail_dev  = weekly_dev_cap.get(wk, 0.0)
        avail_test = weekly_test_cap.get(wk, 0.0)

        # Projekte in Liefertermin-Reihenfolge abarbeiten (Priorität: früheste Due-Date)
        for proj in projects_data:
            pid = proj.project_id
            rem_total = current_impl.get(pid, 0.0) + current_test.get(pid, 0.0)
            if rem_total <= 0:
                continue

            needed_dev  = min(current_impl.get(pid, 0.0), MAX_DEV_PER_PROJECT)
            needed_test = min(current_test.get(pid, 0.0), MAX_TEST_PER_PROJECT)

            alloc_dev  = min(needed_dev,  avail_dev)
            alloc_test = min(needed_test, avail_test)

            avail_dev  -= alloc_dev
            avail_test -= alloc_test

            current_impl[pid] = max(0.0, current_impl.get(pid, 0.0) - alloc_dev)
            current_test[pid] = max(0.0, current_test.get(pid, 0.0) - alloc_test)

        # Burndown-Punkt für diese Woche für alle Projekte eintragen
        for proj in projects_data:
            pid_str = str(proj.project_id)
            remaining = current_impl.get(proj.project_id, 0.0) + current_test.get(proj.project_id, 0.0)
            burndown_data[pid_str].append(BurndownPoint(
                week_key=wk,
                remaining_total=remaining,
            ))

        # Früher beenden, wenn alle Projekte fertig sind
        if all(
            current_impl.get(p.project_id, 0.0) + current_test.get(p.project_id, 0.0) <= 0
            for p in projects_data
        ):
            # Restliche Wochen mit 0 auffüllen, damit alle Serien gleich lang sind
            finished_idx = forecast_weeks.index(wk) + 1
            for future_wk in forecast_weeks[finished_idx:]:
                for proj in projects_data:
                    burndown_data[str(proj.project_id)].append(
                        BurndownPoint(week_key=future_wk, remaining_total=0.0)
                    )
            break

    return ForecastResponse(
        projects=projects_data,
        weeks=full_weeks_list,        # Vorwoche + Forecast-Wochen → gleiche Länge wie Burndown-Punkte
        burndown_data=burndown_data,
    )


# ── API-Endpunkt ───────────────────────────────────────────────────────────────

@router.get("/data", response_model=ForecastResponse)
def get_forecast_data(
    num_projects:            int = Query(5,  ge=1, description="Anzahl Projekte für den Forecast"),
    forecast_weeks_duration: int = Query(52, ge=1, description="Forecast-Horizont in Wochen"),
):
    """
    Liefert die Burndown-Rohdaten für den Forecast.
    Rendering erfolgt separat in charts.py.
    """
    return calculate_forecast(num_projects, forecast_weeks_duration)