"""
routers/milestones.py – Meilenstein-Verwaltung (Tabelle: milestone)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import date
from psycopg2 import IntegrityError
from db import get_cursor

router = APIRouter()

# ── Vordefinierte Farbschemas ──────────────────────────────────────────────────
# Je Schema Hintergrund- + Textfarbe, so gewählt dass ausreichend Kontrast
# (WCAG-tauglich) zwischen Schrift und Hintergrund besteht.
COLOR_SCHEMAS: dict[str, dict] = {
    "blue":   {"label": "Blau",    "bg": "#2874A6", "text": "#FFFFFF"},
    "green":  {"label": "Grün",    "bg": "#1E8449", "text": "#FFFFFF"},
    "purple": {"label": "Violett", "bg": "#7D3C98", "text": "#FFFFFF"},
    "red":    {"label": "Rot",     "bg": "#B03A2E", "text": "#FFFFFF"},
    "amber":  {"label": "Amber",   "bg": "#F1C40F", "text": "#1A1A1A"},
}


# ── Pydantic-Modelle ───────────────────────────────────────────────────────────
class MilestoneBase(BaseModel):
    project_id: int
    milestone_name: str
    color_schema: str
    due_date: date

    @field_validator("color_schema")
    @classmethod
    def validate_color_schema(cls, v):
        if v not in COLOR_SCHEMAS:
            raise ValueError(f"Ungültiges Farbschema. Erlaubt: {', '.join(COLOR_SCHEMAS.keys())}")
        return v


class MilestoneUpdate(BaseModel):
    project_id: Optional[int] = None
    milestone_name: Optional[str] = None
    color_schema: Optional[str] = None
    due_date: Optional[date] = None

    @field_validator("color_schema")
    @classmethod
    def validate_color_schema(cls, v):
        if v is not None and v not in COLOR_SCHEMAS:
            raise ValueError(f"Ungültiges Farbschema. Erlaubt: {', '.join(COLOR_SCHEMAS.keys())}")
        return v


def _with_colors(row: dict) -> dict:
    schema = COLOR_SCHEMAS.get(row["color_schema"], {})
    row["color_bg"]   = schema.get("bg", "#555555")
    row["color_text"] = schema.get("text", "#FFFFFF")
    return row


# ── Endpunkte ──────────────────────────────────────────────────────────────────

@router.get("/color_schemas")
def list_color_schemas():
    """Liefert die 5 vordefinierten Farbschemas (id, label, bg, text)."""
    return [{"id": k, **v} for k, v in COLOR_SCHEMAS.items()]


@router.get("/")
def list_milestones(project_id: Optional[int] = None, project_ids: Optional[str] = None):
    """
    Alle Meilensteine, optional gefiltert nach einem einzelnen project_id
    oder einer kommagetrennten Liste project_ids.
    """
    with get_cursor() as cur:
        conditions = []
        params: list = []

        if project_id is not None:
            conditions.append("m.project_id = %s")
            params.append(project_id)

        if project_ids:
            try:
                ids = [int(x.strip()) for x in project_ids.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(422, f"Ungültige project_ids: {project_ids}")
            if ids:
                conditions.append("m.project_id = ANY(%s)")
                params.append(ids)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cur.execute(f"""
            SELECT m.milestone_id, m.project_id, m.milestone_name,
                   m.color_schema, m.due_date, p.project_name
            FROM milestone m
            JOIN project p ON p.project_id = m.project_id
            {where}
            ORDER BY m.due_date ASC
        """, params)
        rows = [dict(r) for r in cur.fetchall()]

    return [_with_colors(r) for r in rows]


@router.post("/", status_code=201)
def create_milestone(data: MilestoneBase):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT project_id FROM project WHERE project_id = %s", (data.project_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Projekt nicht gefunden")

        try:
            cur.execute("""
                INSERT INTO milestone (project_id, milestone_name, color_schema, due_date)
                VALUES (%s, %s, %s, %s)
                RETURNING milestone_id
            """, (data.project_id, data.milestone_name, data.color_schema, data.due_date))
        except IntegrityError:
            raise HTTPException(
                status_code=409,
                detail="Für dieses Projekt existiert an diesem Datum bereits ein Meilenstein."
            )
        return {"milestone_id": cur.fetchone()["milestone_id"]}


@router.put("/{milestone_id}")
def update_milestone(milestone_id: int, data: MilestoneUpdate):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM milestone WHERE milestone_id = %s", (milestone_id,))
        ex = cur.fetchone()
        if not ex:
            raise HTTPException(status_code=404, detail="Meilenstein nicht gefunden")

        new_project_id = data.project_id if data.project_id is not None else ex["project_id"]
        new_name       = data.milestone_name if data.milestone_name is not None else ex["milestone_name"]
        new_schema     = data.color_schema if data.color_schema is not None else ex["color_schema"]
        new_due        = data.due_date if data.due_date is not None else ex["due_date"]

        if new_project_id != ex["project_id"]:
            cur.execute("SELECT project_id FROM project WHERE project_id = %s", (new_project_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Projekt nicht gefunden")

        try:
            cur.execute("""
                UPDATE milestone
                SET project_id = %s, milestone_name = %s, color_schema = %s, due_date = %s
                WHERE milestone_id = %s
            """, (new_project_id, new_name, new_schema, new_due, milestone_id))
        except IntegrityError:
            raise HTTPException(
                status_code=409,
                detail="Für dieses Projekt existiert an diesem Datum bereits ein Meilenstein."
            )
    return {"milestone_id": milestone_id}


@router.delete("/{milestone_id}")
def delete_milestone(milestone_id: int):
    """
    Meilenstein löschen. Gibt (anders als z.B. bei /api/staff) bewusst 200 mit
    JSON-Body statt 204 zurück, damit das gemeinsam genutzte Gantt-eigene
    api()-Helper (das bei DELETE immer response.json() aufruft) nicht auf
    einem leeren 204-Body scheitert.
    """
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT milestone_id FROM milestone WHERE milestone_id = %s", (milestone_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Meilenstein nicht gefunden")
        cur.execute("DELETE FROM milestone WHERE milestone_id = %s", (milestone_id,))
    return {"milestone_id": milestone_id, "status": "deleted"}