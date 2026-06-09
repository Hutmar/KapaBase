"""
routers/staff.py – CRUD-Endpunkte für Mitarbeiterverwaltung (Tabelle: staff, roles)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from db import get_cursor
from decimal import Decimal
from psycopg2 import IntegrityError

router = APIRouter()

# ── Pydantic-Modelle ───────────────────────────────────────────────────────────

class StaffCreate(BaseModel):
    shortname: str
    hours_per_week: Decimal
    hours_per_day: Optional[Decimal] = None   # wenn None → wird berechnet
    remark: Optional[str] = None
    is_active: bool = True
    roles: List[str] = []                 # z. B. ['Developer', 'Tester']

class StaffUpdate(BaseModel):
    hours_per_week: Optional[Decimal] = None
    hours_per_day: Optional[Decimal] = None   # manuell gesetzt → remark Pflicht
    remark: Optional[str] = None
    is_active: Optional[bool] = None
    roles: Optional[List[str]] = None

# ── Hilfsfunktion ──────────────────────────────────────────────────────────────

def _sync_roles(cur, shortname: str, target_roles: List[str]):
    """
    Synchronisiert die Rollen eines Mitarbeiters schonend.
    Existierende Rollen bleiben unberührt. Neue werden hinzugefügt.
    Gelöschte Rollen werden einzeln entfernt und bei Constraint-Verletzung abgefangen.
    """

    # 1. Aktuelle Rollen aus der DB holen
    cur.execute("SELECT role FROM roles WHERE shortname = %s", (shortname,))
    current_roles = [row["role"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]

    # Sets für den einfachen Abgleich erstellen
    target_set = set(target_roles)
    current_set = set(current_roles)

    # 2. Rollen bestimmen, die hinzugefügt werden müssen
    roles_to_add = target_set - current_set
    for role in roles_to_add:
        cur.execute(
            "INSERT INTO roles (shortname, role) VALUES (%s, %s)",
            (shortname, role)
        )

    # 3. Rollen bestimmen, die gelöscht werden müssen
    roles_to_delete = current_set - target_set
    for role in roles_to_delete:
        try:
            # Nutze einen SAVEPOINT, da ein fehlgeschlagenes DELETE in PostgreSQL
            # sonst die gesamte laufende Transaktion ungültig macht!
            cur.execute(f"SAVEPOINT role_delete_{role}")
            cur.execute(
                "DELETE FROM roles WHERE shortname = %s AND role = %s",
                (shortname, role)
            )
            cur.execute(f"RELEASE SAVEPOINT role_delete_{role}")
        except IntegrityError:
            # Falls das Löschen fehlschlägt (Constraint-Verletzung)
            cur.execute(f"ROLLBACK TO SAVEPOINT role_delete_{role}")
            raise HTTPException(
                status_code=409,
                detail=f"Die Rolle '{role}' kann nicht entfernt werden, da sie noch an anderer Stelle verwendet wird."
            )

# ── Endpunkte ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_staff():
    """Alle Mitarbeiter mit ihren Rollen zurückgeben, inklusive summierter Stunden."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT s.shortname, s.hours_per_week, s.hours_per_day,
            s.remark, s.is_active,
            COALESCE(
                json_agg(r.role ORDER BY r.role)
                FILTER (WHERE r.role IS NOT NULL), '[]'
            ) AS roles
            FROM staff s
            LEFT JOIN roles r ON r.shortname = s.shortname
            GROUP BY s.shortname, s.hours_per_week, s.hours_per_day,
            s.remark, s.is_active
            ORDER BY s.shortname
        """)
        staff_data = cur.fetchall()

        total_hpd_active = Decimal('0.00')
        # Summiere hours_per_day nur für aktive Mitarbeiter
        for s in staff_data:
            if s["is_active"] and s["hours_per_day"] is not None:
                total_hpd_active += s["hours_per_day"]

        total_hpw_calculated = total_hpd_active * Decimal('5') # Teamwoche = Teamtag * 5

        return {
            "staff_members": staff_data,
            "total_hours_per_day": str(total_hpd_active.quantize(Decimal('0.01'))), # Auf 2 Dezimalstellen formatieren
            "total_hours_per_week": str(total_hpw_calculated.quantize(Decimal('0.01'))) # Auf 2 Dezimalstellen formatieren
        }

@router.post("/", status_code=201)
def create_staff(data: StaffCreate):
    """Neuen Mitarbeiter anlegen."""

    # hours_per_day berechnen falls nicht angegeben
    hpd = data.hours_per_day if data.hours_per_day is not None \
        else round(data.hours_per_week / Decimal("5"), 2)

    # Wenn hours_per_day manuell gesetzt → remark Pflicht
    if data.hours_per_day is not None and not data.remark:
        raise HTTPException(status_code=422,
                            detail="Remark ist Pflicht bei manuellem hours_per_day")

    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO staff (shortname, hours_per_week, hours_per_day, remark, is_active)
            VALUES (%s, %s, %s, %s, %s)
        """, (data.shortname, data.hours_per_week, hpd,
              data.remark, data.is_active))
        _sync_roles(cur, data.shortname, data.roles)

    return {"shortname": data.shortname}

@router.put("/{shortname}")
def update_staff(shortname: str, data: StaffUpdate):
    """Mitarbeiter aktualisieren."""
    with get_cursor(commit=True) as cur:
        # Aktuellen Datensatz laden
        cur.execute("SELECT * FROM staff WHERE shortname = %s", (shortname,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Mitarbeiter nicht gefunden")

        # Neue Werte ermitteln
        new_hpw = data.hours_per_week if data.hours_per_week is not None \
            else existing["hours_per_week"]
        new_remark = data.remark if data.remark is not None else existing["remark"]
        new_active = data.is_active if data.is_active is not None \
            else existing["is_active"]

        # 1. Hat der User explizit Stunden pro Tag eingegeben?
        if data.hours_per_day is not None:
            if not new_remark:
                raise HTTPException(
                    status_code=422,
                    detail="Remark ist Pflicht bei manuellem hours_per_day"
                )
            new_hpd = data.hours_per_day

        # 2. Wurden die Wochenstunden übergeben, aber KEIN Tagessatz? (-> Automatische Berechnung)
        elif data.hours_per_week is not None:
            new_hpd = round(new_hpw / Decimal("5"), 2)

        # 3. Nichts von beidem hat sich geändert
        else:
            new_hpd = existing["hours_per_day"]

        cur.execute("""
            UPDATE staff
            SET hours_per_week = %s, hours_per_day = %s,
            remark = %s, is_active = %s
            WHERE shortname = %s
        """, (new_hpw, new_hpd, new_remark, new_active, shortname))

        if data.roles is not None:
            _sync_roles(cur, shortname, data.roles)

        return {"shortname": shortname}

@router.delete("/{shortname}", status_code=204)
def delete_staff(shortname: str):
    """Mitarbeiter löschen (nur wenn keine Referenzen bestehen)."""
    with get_cursor(commit=True) as cur:

        # Prüfen ob Referenzen bestehen
        cur.execute("SELECT COUNT(*) as cnt FROM absence WHERE shortname = %s", (shortname,))
        if cur.fetchone()["cnt"] > 0:
            raise HTTPException(status_code=409,
                                detail="Mitarbeiter hat Abwesenheiten – bitte zuerst löschen")
        cur.execute("DELETE FROM roles   WHERE shortname = %s", (shortname,))
        cur.execute("DELETE FROM staff   WHERE shortname = %s", (shortname,))