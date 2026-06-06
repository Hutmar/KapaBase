"""
routers/projects.py – Projektverwaltung (Tabelle: project)
Inkl. Kapazitätsberechnung und Farbcode-Vorschlag
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
from db import get_cursor
from capacity import calculate_total_capacity

router = APIRouter()


# ── Pydantic-Modelle ───────────────────────────────────────────────────────────

class ProjectBase(BaseModel):
    project_name: str
    customer: str
    jira_id: Optional[str] = None
    target_hours: int
    impl_hours: int
    test_hours: int
    planned: bool = True
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    remarks: Optional[str] = None
    done: bool = False
    color_hexcode: Optional[str] = None
    sort_order: int = 0


class ProjectUpdate(BaseModel):
    project_name: Optional[str] = None
    customer: Optional[str] = None
    jira_id: Optional[str] = None
    target_hours: Optional[int] = None
    impl_hours: Optional[int] = None
    test_hours: Optional[int] = None
    planned: Optional[bool] = None
    start_date: Optional[date] = None
    due_date: Optional[date] = None
    remarks: Optional[str] = None
    done: Optional[bool] = None
    color_hexcode: Optional[str] = None
    sort_order: Optional[int] = None


# ── Hilfsfunktion: nächste freie Farbe ────────────────────────────────────────

PREDEFINED_COLORS = [
    "#4A90D9", "#E67E22", "#2ECC71", "#9B59B6", "#E74C3C",
    "#1ABC9C", "#F39C12", "#3498DB", "#D35400", "#27AE60",
    "#8E44AD", "#C0392B", "#16A085", "#F1C40F", "#2980B9",
]

def _next_free_color() -> str:
    """Schlägt einen noch nicht belegten Farbcode vor."""
    with get_cursor() as cur:
        cur.execute("SELECT color_hexcode FROM project WHERE color_hexcode IS NOT NULL")
        used_p = {r["color_hexcode"] for r in cur.fetchall()}
        cur.execute("SELECT color_hexcode FROM tasks WHERE color_hexcode IS NOT NULL")
        used_t = {r["color_hexcode"] for r in cur.fetchall()}
    used = used_p | used_t
    for c in PREDEFINED_COLORS:
        if c not in used:
            return c
    # Fallback: zufälligen Hex generieren
    import random
    while True:
        c = "#{:06X}".format(random.randint(0, 0xFFFFFF))
        if c not in used:
            return c


# ── Endpunkte ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_projects(
    planned: Optional[bool] = None,
    done: Optional[bool] = False,     # default: nur nicht-erledigte
    search: Optional[str] = None,
):
    """Alle Projekte mit optionalen Filtern und Summen."""
    with get_cursor() as cur:
        conditions = []
        params: list = []

        if planned is not None:
            conditions.append("planned = %s")
            params.append(planned)
        if done is not None:
            conditions.append("done = %s")
            params.append(done)
        if search:
            conditions.append(
                "(project_name ILIKE %s OR customer ILIKE %s OR jira_id ILIKE %s)"
            )
            like = f"%{search}%"
            params.extend([like, like, like])

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cur.execute(f"""
            SELECT * FROM project
            {where}
            ORDER BY sort_order ASC, project_name ASC
        """, params)
        projects = cur.fetchall()

        # Summe target_hours für geplante, nicht erledigte Projekte
        cur.execute("""
            SELECT COALESCE(SUM(target_hours), 0) AS sum_target
            FROM project
            WHERE planned = TRUE AND done = FALSE
        """)
        sum_target = cur.fetchone()["sum_target"]

        # Zeitbereich für Kapazitätsberechnung ermitteln
        cur.execute("""
            SELECT MIN(start_date) AS min_start, MAX(due_date) AS max_due
            FROM project
            WHERE planned = TRUE AND done = FALSE
              AND start_date IS NOT NULL AND due_date IS NOT NULL
        """)
        range_row = cur.fetchone()

    # Kapazität berechnen
    cap_info = {}
    if range_row["min_start"] and range_row["max_due"]:
        cap = calculate_total_capacity(range_row["min_start"], range_row["max_due"])
        cap_info = {
            "period_start": str(range_row["min_start"]),
            "period_end":   str(range_row["max_due"]),
            "total_capacity_hours": cap["total_hours"],
            "diff": sum_target - cap["total_hours"],   # negativ = ausreichend
        }
    else:
        cap_info = {
            "period_start": None, "period_end": None,
            "total_capacity_hours": 0, "diff": sum_target,
        }

    return {
        "projects": projects,
        "sum_target_hours": sum_target,
        "capacity": cap_info,
        "suggested_color": _next_free_color(),
    }


@router.get("/suggest_color")
def suggest_color():
    """Schlägt einen freien Farbcode vor."""
    return {"color": _next_free_color()}


@router.get("/{project_id}")
def get_project(project_id: int):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM project WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Projekt nicht gefunden")
    return row


@router.post("/", status_code=201)
def create_project(data: ProjectBase):
    """Neues Projekt anlegen."""
    if data.impl_hours + data.test_hours != data.target_hours:
        raise HTTPException(status_code=422,
                            detail="impl_hours + test_hours muss target_hours ergeben")

    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO project
              (project_name, customer, jira_id, target_hours, impl_hours, test_hours,
               planned, start_date, due_date, remarks, done, color_hexcode, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING project_id
        """, (data.project_name, data.customer, data.jira_id,
              data.target_hours, data.impl_hours, data.test_hours,
              data.planned, data.start_date, data.due_date,
              data.remarks, data.done, data.color_hexcode, data.sort_order))
        return {"project_id": cur.fetchone()["project_id"]}


@router.put("/{project_id}")
def update_project(project_id: int, data: ProjectUpdate):
    """Projekt aktualisieren (nur gesetzte Felder)."""
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM project WHERE project_id = %s", (project_id,))
        ex = cur.fetchone()
        if not ex:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")

        # Merge mit bestehenden Werten
        fields = {
            "project_name": data.project_name or ex["project_name"],
            "customer":     data.customer     or ex["customer"],
            "jira_id":      data.jira_id      if data.jira_id is not None else ex["jira_id"],
            "target_hours": data.target_hours if data.target_hours is not None else ex["target_hours"],
            "impl_hours":   data.impl_hours   if data.impl_hours   is not None else ex["impl_hours"],
            "test_hours":   data.test_hours   if data.test_hours   is not None else ex["test_hours"],
            "planned":      data.planned      if data.planned      is not None else ex["planned"],
            "start_date":   data.start_date   if data.start_date   is not None else ex["start_date"],
            "due_date":     data.due_date     if data.due_date     is not None else ex["due_date"],
            "remarks":      data.remarks      if data.remarks      is not None else ex["remarks"],
            "done":         data.done         if data.done         is not None else ex["done"],
            "color_hexcode":data.color_hexcode if data.color_hexcode is not None else ex["color_hexcode"],
            "sort_order":   data.sort_order   if data.sort_order   is not None else ex["sort_order"],
        }

        if fields["impl_hours"] + fields["test_hours"] != fields["target_hours"]:
            raise HTTPException(status_code=422,
                                detail="impl_hours + test_hours muss target_hours ergeben")

        cur.execute("""
            UPDATE project SET
              project_name=%s, customer=%s, jira_id=%s, target_hours=%s,
              impl_hours=%s, test_hours=%s, planned=%s, start_date=%s,
              due_date=%s, remarks=%s, done=%s, color_hexcode=%s, sort_order=%s
            WHERE project_id=%s
        """, (fields["project_name"], fields["customer"], fields["jira_id"],
              fields["target_hours"], fields["impl_hours"], fields["test_hours"],
              fields["planned"], fields["start_date"], fields["due_date"],
              fields["remarks"], fields["done"], fields["color_hexcode"],
              fields["sort_order"], project_id))

    return {"project_id": project_id}


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int):
    """Projekt löschen (nur wenn keine Referenzen)."""
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE project_id=%s", (project_id,))
        if cur.fetchone()["cnt"] > 0:
            raise HTTPException(status_code=409, detail="Projekt hat zugeordnete Tasks")
        cur.execute("SELECT COUNT(*) as cnt FROM worked_hours WHERE project_id=%s", (project_id,))
        if cur.fetchone()["cnt"] > 0:
            raise HTTPException(status_code=409, detail="Projekt hat geleistete Stunden")
        cur.execute("DELETE FROM project WHERE project_id = %s", (project_id,))


@router.put("/reorder/bulk")
def reorder_projects(order: List[dict]):
    """
    Bulk-Umsortierung: erwartet Liste von {project_id: int, sort_order: int}
    """
    with get_cursor(commit=True) as cur:
        for item in order:
            cur.execute(
                "UPDATE project SET sort_order=%s WHERE project_id=%s",
                (item["sort_order"], item["project_id"])
            )
    return {"updated": len(order)}