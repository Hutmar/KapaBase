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
from typing import Optional, List
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


def _is_majority_absent(shortname: str, week_key: str,
                         absences: list, at_hols: set) -> bool:
    """True wenn mehr als die Hälfte der Arbeitstage der KW Abwesenheit eingetragen ist."""
    total  = _total_workdays_in_week(week_key, at_hols)
    if total == 0:
        return True
    absent = _absent_workdays_in_week(shortname, week_key, absences, at_hols)
    return absent > total / 2


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
    years = set(range(start.year, end.year + 1))
    hols: set = set()
    for y in years:
        hols |= get_austrian_holidays(y)
    return hols


# ── GET / ──────────────────────────────────────────────────────────────────────

@router.get("/")
def get_planning(
    use_current_week: bool = False,
    end_week: Optional[str] = None,   # Format: 'YYYY-WNN' – wenn gesetzt, bis-KW-Filter
):
    """
    Vollständige Planungsmatrix.

    Startzeitpunkt:
    • use_current_week=True  → aktuelle KW
    • use_current_week=False → min(project.start_date) der geplanten, nicht
      erledigten Projekte; Fallback: aktuelle KW
    Endzeitpunkt: max(project.due_date) der geplanten, nicht erledigten Projekte;
    Fallback: 12 KW nach Start.
    """
    with get_cursor() as cur:

        # ── Zeitbereich aus Projektdaten ────────────────────────────────────
        cur.execute("""
            SELECT MIN(start_date) AS min_start,
                   MAX(due_date)   AS max_due
            FROM project
            WHERE planned = TRUE AND done = FALSE
            AND project_type = 'Project' 
            AND start_date IS NOT NULL AND due_date IS NOT NULL
        """)
        range_row = cur.fetchone()

        today      = date.today()
        today_iso  = today.isocalendar()
        cur_monday = date.fromisocalendar(today_iso[0], today_iso[1], 1)

        if use_current_week or not range_row["min_start"]:
            start_date = cur_monday
        else:
            # Montag der KW in der min_start liegt
            ms     = range_row["min_start"]
            ms_iso = ms.isocalendar()
            start_date = date.fromisocalendar(ms_iso[0], ms_iso[1], 1)

        if range_row["max_due"]:
            md     = range_row["max_due"]
            md_iso = md.isocalendar()
            end_date = date.fromisocalendar(md_iso[0], md_iso[1], 7)
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

        # ── Bestehende Planungen laden ──────────────────────────────────────
        # Planungen im sichtbaren Zeitraum (start_date–end_date überlappend)
        cur.execute("""
            SELECT pl.task_id, pl.project_id, pl.staff, pl.role_id,
                   pl.start_date, pl.end_date,
                   p.project_name, p.color_hexcode AS project_color,
                   t.task_name,    t.color_hexcode AS task_color
            FROM planning pl
            LEFT JOIN project p ON p.project_id = pl.project_id
            LEFT JOIN tasks   t ON t.task_id    = pl.task_id
            WHERE pl.end_date   >= %s
            AND   pl.start_date <= %s
        """, (start_date, end_date))
        plannings_raw = [dict(r) for r in cur.fetchall()]

        # ── Verfügbare Projekte (Drag&Drop) ────────────────────────────────
        # Nur Projekte OHNE Tasks anzeigen (Projekte mit Tasks werden über Tasks geplant)
        # Sortierung: due_date ASC
        cur.execute("""
            SELECT p.project_id, p.project_name, p.customer, p.color_hexcode,
                   p.start_date, p.due_date, p.target_hours, p.impl_hours, p.test_hours
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE
            AND p.project_type = 'Project' 
            ORDER BY p.due_date ASC NULLS LAST, p.project_name ASC
        """)
        all_projects = cur.fetchall()

        # Letztes worked_hours-Datum je Projekt (für outdated-Markierung)
        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

        # ── Verfügbare Tasks (Drag&Drop) ───────────────────────────────────
        cur.execute("""
            SELECT t.task_id, t.task_name, t.color_hexcode, t.project_id,
                   p.project_name
            FROM tasks t
            LEFT JOIN project p ON p.project_id = t.project_id
            ORDER BY t.task_name
        """)
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

        # ── end_week: Enddatum der sichtbaren Matrix begrenzen ───────────────
        # Wenn end_week gesetzt: Matrix nur bis zu dieser KW anzeigen UND
        # available_projects auf Projekte filtern deren start_date VOR dem
        # Montag dieser KW liegt.
        if end_week:
            ew_parts  = end_week.split("-W")
            ew_monday = date.fromisocalendar(int(ew_parts[0]), int(ew_parts[1]), 1)
            # Wochen auf Zeitraum bis end_week kürzen
            weeks = [wk for wk in weeks if wk <= end_week]
            # Projekte filtern
            available_projects = [
                p for p in all_projects
                if p["start_date"] is not None and p["start_date"] < ew_monday
            ]
        else:
            available_projects = list(all_projects)

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
            "available_projects": [dict(p) for p in available_projects],
            "available_tasks":    [dict(t) for t in available_tasks],
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
    • Ist-Liefertermin (letzte KW mit Planung)
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT p.project_id, p.project_name, p.customer,
                   p.target_hours, p.impl_hours AS plan_impl,
                   p.test_hours   AS plan_test,
                   p.due_date, p.color_hexcode
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE
            AND NOT EXISTS (
                SELECT 1 FROM tasks t WHERE t.project_id = p.project_id
            )
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

        # Abwesenheiten für beteiligte Mitarbeiter
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
            min_d = max_d = date.today()

        # Letztes worked_hours-Datum je Projekt
        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day_status = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

        # Feiertage
        at_hols = _build_at_hols(min_d, max_d) if plan_rows else set()

        # Geplante Stunden je Projekt + Rolle aggregieren
        # Veraltete Planungen (neuere worked_hours vorhanden) werden NICHT eingerechnet.
        plan_agg:     dict = {}   # {project_id: {'Developer': float, 'Tester': float}}
        last_end_map: dict = {}   # {project_id: date}

        for pr in plan_rows:
            pid = pr["project_id"]

            # Outdated-Check: gibt es worked_hours mit day > pl.end_date?
            is_outdated = (
                pid in max_worked_day_status
                and max_worked_day_status[pid] > pr["end_date"]
            )
            if is_outdated:
                continue  # diese Planung nicht in die offenen Stunden einrechnen

            wk = iso_week_key(pr["start_date"])
            h  = _effective_hours_in_week(
                pr["staff"], float(pr["hours_per_day"]),
                wk, all_absences, at_hols)
            plan_agg.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
            role_key = pr["role"] if pr["role"] in ("Developer", "Tester") else "Developer"
            plan_agg[pid][role_key] += h

            prev = last_end_map.get(pid)
            if prev is None or pr["end_date"] > prev:
                last_end_map[pid] = pr["end_date"]

        result = []
        for p in projects:
            pid = p["project_id"]
            w   = worked.get(pid, {"worked_impl": 0, "worked_test": 0})
            pa  = plan_agg.get(pid, {"Developer": 0.0, "Tester": 0.0})

            remaining_impl = p["plan_impl"] - float(w["worked_impl"]) - pa["Developer"]
            remaining_test = p["plan_test"] - float(w["worked_test"]) - pa["Tester"]
            diff = remaining_impl + remaining_test

            # Restaufwand = target_hours minus tatsächlich erfasste Stunden
            restaufwand = (
                p["target_hours"]
                - float(w["worked_impl"])
                - float(w["worked_test"])
            )

            # orange entfernt – alles ≤ 0 ist lightgreen
            if diff > 0:
                status_color = "red"
            elif diff == 0:
                status_color = "green"
            else:
                status_color = "lightgreen"

            # Soll-KW aus due_date
            due_kw = None
            if p["due_date"]:
                iso = p["due_date"].isocalendar()
                due_kw = f"{iso[0]}-W{iso[1]:02d}"

            # Ist-KW: letzte KW mit Planung
            last_end = last_end_map.get(pid)
            if last_end:
                iso_e  = last_end.isocalendar()
                ist_kw = f"{iso_e[0]}-W{iso_e[1]:02d}"
            else:
                ist_kw = due_kw

            result.append({
                "project_id":      pid,
                "project_name":    p["project_name"],
                "customer":        p["customer"],
                "color_hexcode":   p["color_hexcode"],
                "target_hours":    p["target_hours"],
                "restaufwand":     restaufwand,
                "due_date":        str(p["due_date"]) if p["due_date"] else None,
                "due_kw":          due_kw,
                "ist_kw":          ist_kw,
                "remaining_impl":  remaining_impl,
                "remaining_test":  remaining_test,
                "remaining_hours": diff,
                "status_color":    status_color,
            })

        return result