"""
routers/worked_hours.py – Stundenerfassung (Tabelle: worked_hours)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date
from db import get_cursor

router = APIRouter()


class WorkedHoursEntry(BaseModel):
    project_id: int
    day: date
    impl_hours: int = 0
    test_hours: int = 0


class WorkedHoursUpdate(BaseModel):
    day: Optional[date] = None
    impl_hours: Optional[int] = None
    test_hours: Optional[int] = None


@router.get("/{project_id}")
def list_worked_hours(project_id: int):
    """Geleistete Stunden und Projekt-Metadaten laden."""
    with get_cursor() as cur:
        # 1. Projektdetails (Soll-Werte) direkt aus der Projekttabelle holen
        cur.execute("""
            SELECT project_name, target_hours, impl_hours AS plan_impl, test_hours AS plan_test
            FROM project
            WHERE project_id = %s
        """, (project_id,))
        project_info = cur.fetchone()
        
        if not project_info:
            raise HTTPException(404, "Projekt nicht gefunden")
            
        if isinstance(project_info, tuple):
            project_info = {
                "project_name": project_info[0],
                "target_hours": project_info[1],
                "plan_impl": project_info[2],
                "plan_test": project_info[3]
            }

        # 2. Reine Ist-Stunden absteigend nach Datum laden (ohne redundanten Spalten-Mischmasch)
        cur.execute("""
            SELECT worked_hours_id, project_id, day, impl_hours, test_hours
            FROM worked_hours
            WHERE project_id = %s
            ORDER BY day DESC
        """, (project_id,))
        rows = cur.fetchall()

        # 3. Summen der Ist-Stunden berechnen
        cur.execute("""
            SELECT COALESCE(SUM(impl_hours), 0) AS sum_impl,
                   COALESCE(SUM(test_hours), 0) AS sum_test
            FROM worked_hours WHERE project_id = %s
        """, (project_id,))
        totals = cur.fetchone()

    # Wir geben die Projekt-Metadaten jetzt als separates Objekt "project" zurück
    return {
        "project": project_info, 
        "entries": rows, 
        "totals": totals
    }

@router.post("/", status_code=201)
def create_worked_hours(data: WorkedHoursEntry):
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO worked_hours (project_id, day, impl_hours, test_hours)
            VALUES (%s, %s, %s, %s) RETURNING worked_hours_id
        """, (data.project_id, data.day, data.impl_hours, data.test_hours))
        return {"worked_hours_id": cur.fetchone()["worked_hours_id"]}


@router.put("/{wh_id}")
def update_worked_hours(wh_id: int, data: WorkedHoursUpdate):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM worked_hours WHERE worked_hours_id = %s", (wh_id,))
        ex = cur.fetchone()
        if not ex:
            raise HTTPException(404, "Eintrag nicht gefunden")
        cur.execute("""
            UPDATE worked_hours
            SET day=%s, impl_hours=%s, test_hours=%s
            WHERE worked_hours_id=%s
        """, (data.day or ex["day"],
              data.impl_hours if data.impl_hours is not None else ex["impl_hours"],
              data.test_hours if data.test_hours is not None else ex["test_hours"],
              wh_id))
    return {"worked_hours_id": wh_id}


@router.delete("/{wh_id}", status_code=204)
def delete_worked_hours(wh_id: int):
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM worked_hours WHERE worked_hours_id = %s", (wh_id,))