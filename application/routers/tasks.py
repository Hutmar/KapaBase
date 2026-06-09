"""
routers/tasks.py – Task-Verwaltung (Tabelle: tasks)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from db import get_cursor

router = APIRouter()


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
            cur.execute("""
                SELECT t.*, p.project_name FROM tasks t
                LEFT JOIN project p ON p.project_id = t.project_id
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
    # Validierung vor dem Insert
    _validate_project_type(data.project_id)
    
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO tasks (project_id, task_name, color_hexcode)
            VALUES (%s, %s, %s) RETURNING task_id
        """, (data.project_id, data.task_name, data.color_hexcode))
        return {"task_id": cur.fetchone()["task_id"]}


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
def delete_task(task_id: int):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM planning WHERE task_id = %s", (task_id,))
        if cur.fetchone()["cnt"] > 0:
            raise HTTPException(409, "Task ist in Planung referenziert")
        cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))