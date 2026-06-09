"""
routers/absence.py – Abwesenheitsverwaltung (Tabelle: absence)
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import date, timedelta
from db import get_cursor

router = APIRouter()

# ── Pydantic-Modelle ───────────────────────────────────────────────────────────
class AbsenceBase(BaseModel):
    shortname: str
    absence_from: date
    absence_to: date
    absence_type: str   # Enum-Wert aus absenceType

class AbsenceCreate(AbsenceBase):
    pass

class AbsenceUpdate(BaseModel):
    absence_from: Optional[date] = None
    absence_to: Optional[date] = None
    absence_type: Optional[str] = None

# ── Hilfsfunktion: Überlappungsprüfung ────────────────────────────────────────
def _check_overlap(cur, shortname: str, absence_from: date,
                   absence_to: date, exclude_id: Optional[int] = None):
    """Wirft HTTPException 409 wenn Überschneidung gefunden."""
    sql = """
    SELECT absence_id FROM absence
    WHERE shortname = %s
    AND absence_from <= %s
    AND absence_to   >= %s
    """
    params = [shortname, absence_to, absence_from]
    if exclude_id is not None:
        sql += " AND absence_id != %s"
        params.append(exclude_id)

    cur.execute(sql, params)
    if cur.fetchone():
        raise HTTPException(
            status_code=409,
            detail="Überschneidung mit bestehender Abwesenheit gefunden"
        )

# ── Endpunkte ──────────────────────────────────────────────────────────────────
@router.get("/")
def list_absences(shortname: Optional[str] = None):
    """Alle Abwesenheiten, optional gefiltert nach Mitarbeiter."""
    with get_cursor() as cur:
        if shortname:
            cur.execute("""
                SELECT * FROM absence
                WHERE shortname = %s
                ORDER BY absence_from DESC
            """, (shortname,))
        else:
            cur.execute("""
                SELECT * FROM absence
                ORDER BY shortname, absence_from DESC
            """)
        return cur.fetchall()

@router.get("/summary", response_model=List[Dict[str, Any]])
def get_absence_summary(shortname: Optional[str] = None):
    """
    Gibt eine Übersicht über Abwesenheitstage pro Mitarbeiter zurück.
    Umfasst Urlaubstage, gesamte Abwesenheitstage sowie erste und letzte Abwesenheit.
    """
    with get_cursor() as cur:
        query = """
            SELECT
                shortname,
                SUM(CASE WHEN absence_type = 'Urlaub' THEN (absence_to - absence_from + 1) ELSE 0 END) AS vacation_days,
                SUM(absence_to - absence_from + 1) AS total_absence_days,
                MIN(absence_from) AS first_absence,
                MAX(absence_to) AS last_absence
            FROM
                absence
        """
        params = []
        if shortname:
            query += " WHERE shortname = %s"
            params.append(shortname)
        
        query += " GROUP BY shortname ORDER BY shortname"

        cur.execute(query, params)
        return cur.fetchall()

@router.post("/", status_code=201)
def create_absence(data: AbsenceCreate):
    """Neue Abwesenheit anlegen, mit Überlappungsprüfung."""
    if data.absence_to < data.absence_from:
        raise HTTPException(status_code=422,
                            detail="Enddatum muss nach Startdatum liegen")

    with get_cursor(commit=True) as cur:
        _check_overlap(cur, data.shortname, data.absence_from, data.absence_to)
        cur.execute("""
            INSERT INTO absence (shortname, absence_from, absence_to, absence_type)
            VALUES (%s, %s, %s, %s)
            RETURNING absence_id
        """, (data.shortname, data.absence_from, data.absence_to, data.absence_type))
        row = cur.fetchone()
        return {"absence_id": row["absence_id"]}

@router.put("/{absence_id}")
def update_absence(absence_id: int, data: AbsenceUpdate):
    """Abwesenheit aktualisieren, mit Überlappungsprüfung."""
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM absence WHERE absence_id = %s", (absence_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Abwesenheit nicht gefunden")

        new_from  = data.absence_from  or existing["absence_from"]
        new_to    = data.absence_to    or existing["absence_to"]
        new_type  = data.absence_type  or existing["absence_type"]

        if new_to < new_from:
            raise HTTPException(status_code=422,
                                detail="Enddatum muss nach Startdatum liegen")

        _check_overlap(cur, existing["shortname"], new_from, new_to,
                       exclude_id=absence_id)

        cur.execute("""
            UPDATE absence
            SET absence_from = %s, absence_to = %s, absence_type = %s
            WHERE absence_id = %s
        """, (new_from, new_to, new_type, absence_id))

        return {"absence_id": absence_id}

@router.delete("/{absence_id}", status_code=204)
def delete_absence(absence_id: int):
    """Abwesenheit löschen."""
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM absence WHERE absence_id = %s", (absence_id,))