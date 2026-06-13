"""
routers/planning_variants.py – Planungsvarianten (Tabelle: planning_variant)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from db import get_cursor

router = APIRouter()


class VariantCreate(BaseModel):
    variant_name: Optional[str] = None


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
    """Neue Planungsvariante anlegen."""
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO planning_variant (variant_name, is_active)
            VALUES (%s, FALSE)
            RETURNING variant_id
        """, (data.variant_name,))
        return {"variant_id": cur.fetchone()["variant_id"]}


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

        cur.execute("UPDATE planning_variant SET is_active = FALSE")
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
