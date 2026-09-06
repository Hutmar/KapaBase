"""
routers/default_task.py – Standard-Task-Verwaltung (Tabelle: default_task)

Ein Mitarbeiter kann einen oder mehrere Standard-Task-Einträge haben:
- Entweder GENAU EINEN Eintrag OHNE Zeitbegrenzung (active_from und
  active_to beide NULL) – dann darf kein weiterer Eintrag existieren.
- Oder MEHRERE zeitlich begrenzte Einträge, deren Zeiträume sich nicht
  überlappen dürfen (NULL bei active_from/active_to bedeutet dabei
  "unbegrenzt in die jeweilige Richtung", z.B. "ab dem X, kein Enddatum").

Der Standard-Task wird in der Planungsmatrix (routers/planning.py) für
eine gegebene Kalenderwoche nur dann angezeigt, wenn ein Eintrag existiert,
dessen Zeitraum die GESAMTE Woche (Mo–Fr) abdeckt.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date
from db import get_cursor

router = APIRouter()


class DefaultTaskCreate(BaseModel):
    shortname:   str
    task_id:     int
    active_from: Optional[date] = None
    active_to:   Optional[date] = None


class DefaultTaskUpdate(BaseModel):
    task_id:     Optional[int] = None
    # active_from/active_to werden vom Frontend beim Speichern IMMER explizit
    # mitgesendet (auch null, um eine Zeitbegrenzung zu entfernen) – daher
    # werden sie unten direkt übernommen statt nur bei "not None" gemerged
    # (analog zum staff.active_from/active_to-Muster in routers/staff.py).
    active_from: Optional[date] = None
    active_to:   Optional[date] = None


# ── Hilfsfunktion: Überlappungsprüfung ────────────────────────────────────────
def _check_default_task_overlap(cur, shortname: str,
                                active_from: Optional[date], active_to: Optional[date],
                                exclude_id: Optional[int] = None):
    """
    Stellt sicher, dass es für einen Mitarbeiter zu jedem Zeitpunkt höchstens
    einen gültigen Standard-Task gibt (siehe Modul-Docstring für die Regeln).
    """
    cur.execute("""
        SELECT default_task_id, active_from, active_to
        FROM default_task
        WHERE shortname = %s
    """, (shortname,))
    existing = [r for r in cur.fetchall() if exclude_id is None or r["default_task_id"] != exclude_id]

    new_is_unlimited = active_from is None and active_to is None

    if new_is_unlimited:
        if existing:
            raise HTTPException(
                status_code=409,
                detail="Es existieren bereits Standard-Task-Einträge für diesen Mitarbeiter – "
                       "ein Eintrag ohne Zeitbegrenzung ist nur möglich, wenn keine anderen "
                       "Einträge vorhanden sind."
            )
        return

    for e in existing:
        e_unlimited = e["active_from"] is None and e["active_to"] is None
        if e_unlimited:
            raise HTTPException(
                status_code=409,
                detail="Es existiert bereits ein Standard-Task ohne Zeitbegrenzung für diesen "
                       "Mitarbeiter – ein zusätzlicher zeitlich begrenzter Eintrag ist nicht möglich."
            )
        # Überlappungsprüfung: NULL wird als "unbegrenzt" in die jeweilige
        # Richtung behandelt (date.min/date.max als Ersatzwerte für den Vergleich).
        e_from = e["active_from"] or date.min
        e_to   = e["active_to"]   or date.max
        n_from = active_from or date.min
        n_to   = active_to   or date.max
        if n_from <= e_to and n_to >= e_from:
            raise HTTPException(
                status_code=409,
                detail=f"Zeitraum überschneidet sich mit bestehendem Standard-Task-Eintrag "
                       f"({e['active_from'] or '–'} bis {e['active_to'] or '–'})."
            )


# ── Endpunkte ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_default_tasks(shortname: Optional[str] = None):
    """Standard-Task-Einträge auflisten, optional gefiltert nach Mitarbeiter."""
    with get_cursor() as cur:
        conditions = []
        params: list = []
        if shortname:
            conditions.append("dt.shortname = %s")
            params.append(shortname)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cur.execute(f"""
            SELECT dt.default_task_id, dt.shortname, dt.task_id,
                   dt.active_from, dt.active_to,
                   t.task_name, t.color_hexcode AS task_color,
                   p.project_name
            FROM default_task dt
            JOIN tasks t ON t.task_id = dt.task_id
            LEFT JOIN project p ON p.project_id = t.project_id
            {where}
            ORDER BY dt.shortname, dt.active_from ASC NULLS FIRST
        """, params)
        return cur.fetchall()


@router.post("/", status_code=201)
def create_default_task(data: DefaultTaskCreate):
    """Neuen Standard-Task-Eintrag anlegen, mit Überlappungsprüfung."""
    if data.active_from is not None and data.active_to is not None and data.active_to < data.active_from:
        raise HTTPException(status_code=422, detail="Bis-Datum muss nach Von-Datum liegen")

    with get_cursor(commit=True) as cur:
        cur.execute("SELECT shortname FROM staff WHERE shortname = %s", (data.shortname,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Mitarbeiter nicht gefunden")

        cur.execute("SELECT task_id FROM tasks WHERE task_id = %s", (data.task_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Task nicht gefunden")

        _check_default_task_overlap(cur, data.shortname, data.active_from, data.active_to)

        cur.execute("""
            INSERT INTO default_task (shortname, task_id, active_from, active_to)
            VALUES (%s, %s, %s, %s)
            RETURNING default_task_id
        """, (data.shortname, data.task_id, data.active_from, data.active_to))
        row = cur.fetchone()

    return {"default_task_id": row["default_task_id"]}


@router.put("/{default_task_id}")
def update_default_task(default_task_id: int, data: DefaultTaskUpdate):
    """Standard-Task-Eintrag aktualisieren (Task und/oder Zeitraum), mit Überlappungsprüfung."""
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM default_task WHERE default_task_id = %s", (default_task_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Standard-Task-Eintrag nicht gefunden")

        new_task_id     = data.task_id if data.task_id is not None else existing["task_id"]
        new_active_from = data.active_from
        new_active_to   = data.active_to

        if new_task_id != existing["task_id"]:
            cur.execute("SELECT task_id FROM tasks WHERE task_id = %s", (new_task_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Task nicht gefunden")

        if new_active_to is not None and new_active_from is not None and new_active_to < new_active_from:
            raise HTTPException(status_code=422, detail="Bis-Datum muss nach Von-Datum liegen")

        _check_default_task_overlap(
            cur, existing["shortname"], new_active_from, new_active_to,
            exclude_id=default_task_id
        )

        cur.execute("""
            UPDATE default_task
            SET task_id = %s, active_from = %s, active_to = %s
            WHERE default_task_id = %s
        """, (new_task_id, new_active_from, new_active_to, default_task_id))

    return {"default_task_id": default_task_id}


@router.delete("/{default_task_id}")
def delete_default_task(default_task_id: int):
    """Standard-Task-Eintrag löschen. Gibt 200 + JSON zurück (nicht 204)."""
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT default_task_id FROM default_task WHERE default_task_id = %s", (default_task_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Standard-Task-Eintrag nicht gefunden")
        cur.execute("DELETE FROM default_task WHERE default_task_id = %s", (default_task_id,))

    return {"default_task_id": default_task_id, "status": "deleted"}
