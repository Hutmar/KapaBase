"""
routers/tasks.py – Task-Verwaltung (Tabelle: tasks)
"""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from psycopg2 import IntegrityError
from db import get_cursor

router = APIRouter()
logger = logging.getLogger(__name__)


class TaskBase(BaseModel):
    project_id: Optional[int] = None
    task_name: str
    color_hexcode: Optional[str] = None


class TaskUpdate(BaseModel):
    project_id: Optional[int] = None
    task_name: Optional[str] = None
    color_hexcode: Optional[str] = None


PREDEFINED_COLORS = [
    "#F0A500", "#C0392B", "#8E44AD", "#1ABC9C", "#2980B9",
    "#D35400", "#27AE60", "#F39C12", "#16A085", "#E74C3C",
    "#9B59B6", "#3498DB", "#2ECC71", "#E67E22", "#4A90D9",
]


def _next_free_color() -> str:
    with get_cursor() as cur:
        cur.execute("SELECT color_hexcode FROM project WHERE color_hexcode IS NOT NULL")
        used_p = {r["color_hexcode"] for r in cur.fetchall()}
        cur.execute("SELECT color_hexcode FROM tasks WHERE color_hexcode IS NOT NULL")
        used_t = {r["color_hexcode"] for r in cur.fetchall()}
    used = used_p | used_t
    for c in PREDEFINED_COLORS:
        if c not in used:
            return c
    import random
    while True:
        c = "#{:06X}".format(random.randint(0, 0xFFFFFF))
        if c not in used:
            return c


def _increment_color(hex_code: Optional[str]) -> str:
    """
    Inkrementiert einen Hexcode um 1 (z.B. '#4A90D9' -> '#4A90DA', mit
    Überlauf zurück auf '#000000'). Analog zu routers/projects.py –
    wird verwendet, um bei einer Unique-Constraint-Verletzung auf
    tasks.color_hexcode automatisch einen freien Farbcode zu finden, ohne
    dem Nutzer im Dialog nur einen generischen "Internal Server Error"
    anzuzeigen.
    """
    if not hex_code or not hex_code.startswith("#") or len(hex_code) != 7:
        hex_code = "#000000"
    try:
        value = int(hex_code[1:], 16)
    except ValueError:
        value = 0
    value = (value + 1) % 0x1000000
    return "#{:06X}".format(value)


@router.get("/")
def list_tasks(project_id: Optional[int] = None):
    with get_cursor() as cur:
        if project_id is not None:
            cur.execute("""
                SELECT t.*, p.project_name FROM tasks t
                LEFT JOIN project p ON p.project_id = t.project_id
                WHERE t.project_id = %s ORDER BY t.task_name
            """, (project_id,))
        else:
            # Tasks ohne Projekt (t.project_id IS NULL) müssen hier ebenfalls
            # sichtbar sein – sonst verschwinden projektlose Tasks aus der
            # "Alle"-Ansicht, obwohl sie angelegt werden können.
            cur.execute("""
                SELECT t.*, p.project_name FROM tasks t
                LEFT JOIN project p ON p.project_id = t.project_id
                WHERE t.project_id IS NULL
                   OR p.project_type IN ('Operations', 'Internal')
                ORDER BY t.task_name
            """)
        rows = cur.fetchall()
    return {"tasks": rows, "suggested_color": _next_free_color()}


# Hilfsfunktion zur Validierung des Projekttyps (in routers/tasks.py einfügen)
def _validate_project_type(project_id: Optional[int]):
    if project_id is None:
        return
    with get_cursor() as cur:
        # Ersetze 'project_type' durch die tatsächliche Spaltenbezeichnung in deiner DB
        cur.execute("SELECT project_type FROM project WHERE project_id = %s", (project_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")
        if row["project_type"] not in ["Operations", "Internal"]:
            raise HTTPException(
                status_code=422, 
                detail="Tasks dürfen nur Projekten vom Typ 'Operations' oder 'Internal' zugeordnet werden."
            )

@router.post("/", status_code=201)
def create_task(data: TaskBase):
    # Validierung vor dem Insert (bei project_id=None wird nichts geprüft –
    # Tasks können also ohne Projekt angelegt werden)
    _validate_project_type(data.project_id)

    with get_cursor(commit=True) as cur:
        color = data.color_hexcode
        attempts = 0
        new_id = None

        while True:
            try:
                cur.execute("SAVEPOINT color_insert_task")
                cur.execute("""
                    INSERT INTO tasks (project_id, task_name, color_hexcode)
                    VALUES (%s, %s, %s) RETURNING task_id
                """, (data.project_id, data.task_name, color))
                new_id = cur.fetchone()["task_id"]
                cur.execute("RELEASE SAVEPOINT color_insert_task")
                break
            except IntegrityError as exc:
                cur.execute("ROLLBACK TO SAVEPOINT color_insert_task")
                logger.warning(
                    "Farbcode %s beim Anlegen von Task '%s' bereits vergeben (%s) – "
                    "inkrementiere automatisch auf nächsten freien Wert.",
                    color, data.task_name, exc
                )
                color = _increment_color(color)
                attempts += 1
                if attempts > 500:
                    logger.error("Konnte für Task '%s' keinen freien Farbcode finden.", data.task_name)
                    raise HTTPException(status_code=500, detail="Konnte keinen freien Farbcode finden")

        return {"task_id": new_id}


@router.put("/{task_id}")
def update_task(task_id: int, data: TaskUpdate):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
        ex = cur.fetchone()
        if not ex:
            raise HTTPException(404, "Task nicht gefunden")
        
        # Bestimmen, welche Projekt-ID am Ende gesetzt werden soll
        target_project_id = data.project_id if data.project_id is not None else ex["project_id"]
        
        # Validierung vor dem Update (nur wenn sich das Projekt geändert hat oder neu gesetzt wird)
        if data.project_id is not None:
            _validate_project_type(target_project_id)

        cur.execute("""
            UPDATE tasks SET
              project_id   = %s,
              task_name    = %s,
              color_hexcode= %s
            WHERE task_id = %s
        """, (target_project_id,
              data.task_name    or ex["task_name"],
              data.color_hexcode if data.color_hexcode is not None else ex["color_hexcode"],
              task_id))
    return {"task_id": task_id}


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int, force: bool = False):
    """
    Task löschen. Sind Planungseinträge referenziert, wird ohne force=True
    ein 409-Konflikt mit "PLANNING_CONFLICT:"-Präfix zurückgegeben (analog
    zum Rollen-Löschen in routers/staff.py), damit das Frontend eine
    Bestätigungsabfrage anzeigen und bei Bestätigung mit force=True erneut
    aufrufen kann.
    """
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM planning WHERE task_id = %s", (task_id,))
        cnt = cur.fetchone()["cnt"]

        if cnt > 0:
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail=f"PLANNING_CONFLICT:Für diesen Task existieren {cnt} Planungseinträge. "
                           f"Sollen diese ebenfalls gelöscht werden?"
                )
            cur.execute("DELETE FROM planning WHERE task_id = %s", (task_id,))

        # Standard-Task-Einträge (Tabelle default_task), die auf diesen Task
        # verweisen, werden durch ON DELETE CASCADE automatisch mitgelöscht
        # (siehe database.sql).
        cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
