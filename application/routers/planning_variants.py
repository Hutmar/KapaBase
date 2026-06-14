"""
routers/planning_variants.py – Planungsvarianten (Tabelle: planning_variant)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date
from db import get_cursor

router = APIRouter()


class VariantCreate(BaseModel):
    variant_name: Optional[str] = None
    copy_from_variant_id: Optional[int] = None   # NEU: Quell-Variante


class VariantUpdate(BaseModel):
    variant_name: Optional[str] = None


@router.get("/")
def list_variants():
    """Alle Planungsvarianten auflisten."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT variant_id, variant_name, is_active, created_at, active_since
            FROM planning_variant
            ORDER BY created_at DESC
        """)
        return cur.fetchall()


@router.post("/", status_code=201)
def create_variant(data: VariantCreate):
    """
    Neue Planungsvariante anlegen.
    Falls copy_from_variant_id angegeben, werden alle Planungseinträge der
    Quell-Variante in die neue Variante kopiert.
    """
    with get_cursor(commit=True) as cur:

        # Quell-Variante prüfen (falls angegeben)
        if data.copy_from_variant_id is not None:
            cur.execute(
                "SELECT variant_id FROM planning_variant WHERE variant_id = %s",
                (data.copy_from_variant_id,)
            )
            if not cur.fetchone():
                raise HTTPException(
                    status_code=404,
                    detail=f"Quell-Variante {data.copy_from_variant_id} nicht gefunden."
                )

        # Neue Variante anlegen
        cur.execute("""
            INSERT INTO planning_variant (variant_name, is_active)
            VALUES (%s, FALSE)
            RETURNING variant_id
        """, (data.variant_name,))
        new_variant_id = cur.fetchone()["variant_id"]

        copied_rows = 0

        # Planungseinträge der Quell-Variante kopieren (nur ab aktuelle KW)
        if data.copy_from_variant_id is not None:
            today = date.today()
            iso   = today.isocalendar()
            current_week_monday = date.fromisocalendar(iso[0], iso[1], 1)

            cur.execute("""
                INSERT INTO planning
                    (task_id, project_id, staff, role_id, variant_id, start_date, end_date)
                SELECT
                    task_id, project_id, staff, role_id, %s, start_date, end_date
                FROM planning
                WHERE variant_id = %s
                  AND start_date >= %s
            """, (new_variant_id, data.copy_from_variant_id, current_week_monday))
            copied_rows = cur.rowcount

        return {"variant_id": new_variant_id, "copied_rows": copied_rows}


@router.put("/{variant_id}")
def update_variant(variant_id: int, data: VariantUpdate):
    """Variantenname aktualisieren."""
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT variant_id FROM planning_variant WHERE variant_id = %s", (variant_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Variante nicht gefunden")
        cur.execute("""
            UPDATE planning_variant SET variant_name = %s WHERE variant_id = %s
        """, (data.variant_name, variant_id))
    return {"variant_id": variant_id}


@router.post("/{variant_id}/activate")
def activate_variant(variant_id: int):
    """
    Gewählte Variante aktivieren.
    In einer Transaktion: alle auf FALSE, dann die gewählte auf TRUE + active_since = NOW().
    """
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT variant_id FROM planning_variant WHERE variant_id = %s", (variant_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Variante nicht gefunden")

        cur.execute("UPDATE planning_variant SET is_active = FALSE, active_since = NULL")
        cur.execute("""
            UPDATE planning_variant
            SET is_active = TRUE, active_since = NOW()
            WHERE variant_id = %s
        """, (variant_id,))
    return {"variant_id": variant_id, "status": "activated"}


@router.delete("/{variant_id}")
def delete_variant(variant_id: int):
    """
    Variante löschen (nur inaktive).
    Löscht zuerst alle planning-Einträge dieser Variante.
    """
    with get_cursor(commit=True) as cur:
        cur.execute(
            "SELECT variant_id, is_active FROM planning_variant WHERE variant_id = %s",
            (variant_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Variante nicht gefunden")
        if row["is_active"]:
            raise HTTPException(
                status_code=409,
                detail="Aktive Variante kann nicht gelöscht werden"
            )

        cur.execute("DELETE FROM planning WHERE variant_id = %s", (variant_id,))
        cur.execute("DELETE FROM planning_variant WHERE variant_id = %s", (variant_id,))

    return {"variant_id": variant_id, "status": "deleted"}
