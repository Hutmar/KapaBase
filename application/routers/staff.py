"""
routers/staff.py – CRUD-Endpunkte für Mitarbeiterverwaltung (Tabelle: staff, roles)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date
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
    default_task_id: Optional[int] = None # Standard-Task (nur Anzeige in Planung, nicht gespeichert)
    active_from: Optional[date] = None    # Aktiv ab (optional, NULL = unbegrenzt in die Vergangenheit)
    active_to: Optional[date] = None      # Aktiv bis (optional, NULL = unbegrenzt in die Zukunft)

class StaffUpdate(BaseModel):
    hours_per_week: Optional[Decimal] = None
    hours_per_day: Optional[Decimal] = None   # NUR gesetzt, wenn der Nutzer h/Tag manuell geändert hat → remark Pflicht
    remark: Optional[str] = None
    is_active: Optional[bool] = None
    roles: Optional[List[str]] = None
    force_delete_plannings: Optional[bool] = False
    default_task_id: Optional[int] = None     # Standard-Task; wird immer gesetzt (auch null = entfernen)
    active_from: Optional[date] = None        # Aktiv ab; wird immer gesetzt (auch null = entfernen)
    active_to: Optional[date] = None          # Aktiv bis; wird immer gesetzt (auch null = entfernen)

# ── Hilfsfunktion ──────────────────────────────────────────────────────────────

def _sync_roles(cur, shortname: str, target_roles: List[str], force_delete_plannings: bool = False):
    """
    Synchronisiert die Rollen eines Mitarbeiters schonend.
    Existierende Rollen bleiben unberührt. Neue werden hinzugefügt.
    Gelöschte Rollen werden einzeln entfernt und bei Constraint-Verletzung abgefangen.
    """

    # 1. Aktuelle Rollen aus der DB holen
    cur.execute("SELECT role_id, role FROM roles WHERE shortname = %s", (shortname,))
    db_rows = cur.fetchall()
    
    current_roles = [row["role"] if isinstance(row, dict) else row[1] for row in db_rows]
    role_id_map = {
        (row["role"] if isinstance(row, dict) else row[1]): (row["role_id"] if isinstance(row, dict) else row[0])
        for row in db_rows
    }

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
        role_id = role_id_map.get(role)
        if role_id:
            # Prüfen, ob Planungen für diese Rolle existieren
            cur.execute(
                "SELECT start_date FROM planning WHERE staff = %s AND role_id = %s",
                (shortname, role_id)
            )
            plannings = cur.fetchall()
            
            if plannings:
                if not force_delete_plannings:
                    # Eindeutige KW-Nummern extrahieren
                    weeks = sorted(list({
                        (p["start_date"] if isinstance(p, dict) else p[0]).isocalendar()[1]
                        for p in plannings
                    }))
                    weeks_str = ", ".join(map(str, weeks))
                    raise HTTPException(
                        status_code=409,
                        detail=f"PLANNING_CONFLICT:Für die Rolle {role} existieren Planungen für diese Kalenderwochen: {weeks_str}. Sollen diese Planungen enfernt werden?"
                    )
                else:
                    # Planungen löschen, da force_delete_plannings=True
                    cur.execute(
                        "DELETE FROM planning WHERE staff = %s AND role_id = %s",
                        (shortname, role_id)
                    )

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


def _cleanup_out_of_window_absences_and_plannings(cur, shortname: str,
                                                   active_from: Optional[date],
                                                   active_to: Optional[date]):
    """
    Entfernt Abwesenheiten und Planungseinträge des Mitarbeiters, die
    (teilweise) außerhalb des (neuen) aktiven Zeitraums [active_from, active_to]
    liegen. Wird aufgerufen, wenn active_from und/oder active_to nachträglich
    gesetzt bzw. verändert werden.

    NULL bei active_from/active_to bedeutet "unbegrenzt" in die jeweilige
    Richtung – in dieser Richtung wird dann nichts bereinigt.

    Gibt (anzahl_geloeschter_abwesenheiten, anzahl_geloeschter_planungen) zurück.
    """
    deleted_absences = 0
    deleted_plannings = 0

    if active_from is not None:
        cur.execute(
            "DELETE FROM absence WHERE shortname = %s AND absence_from < %s",
            (shortname, active_from)
        )
        deleted_absences += cur.rowcount
        cur.execute(
            "DELETE FROM planning WHERE staff = %s AND start_date < %s",
            (shortname, active_from)
        )
        deleted_plannings += cur.rowcount

    if active_to is not None:
        cur.execute(
            "DELETE FROM absence WHERE shortname = %s AND absence_to > %s",
            (shortname, active_to)
        )
        deleted_absences += cur.rowcount
        cur.execute(
            "DELETE FROM planning WHERE staff = %s AND end_date > %s",
            (shortname, active_to)
        )
        deleted_plannings += cur.rowcount

    return deleted_absences, deleted_plannings

# ── Endpunkte ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_staff():
    """Alle Mitarbeiter mit ihren Rollen zurückgeben, inklusive summierter Stunden."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT s.shortname, s.hours_per_week, s.hours_per_day,
            s.remark, s.is_active, s.default_task_id, dt.task_name AS default_task_name,
            s.active_from, s.active_to,
            COALESCE(
                json_agg(r.role ORDER BY r.role)
                FILTER (WHERE r.role IS NOT NULL), '[]'
            ) AS roles
            FROM staff s
            LEFT JOIN roles r ON r.shortname = s.shortname
            LEFT JOIN tasks dt ON dt.task_id = s.default_task_id
            GROUP BY s.shortname, s.hours_per_week, s.hours_per_day,
            s.remark, s.is_active, s.default_task_id, dt.task_name,
            s.active_from, s.active_to
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

    # Wird ein aktiver Zeitraum (active_from/active_to) angegeben, muss der
    # Mitarbeiter zwingend als aktiv (is_active = TRUE) angelegt werden.
    is_active = data.is_active
    if data.active_from is not None or data.active_to is not None:
        is_active = True

    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO staff (shortname, hours_per_week, hours_per_day, remark, is_active,
                                default_task_id, active_from, active_to)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (data.shortname, data.hours_per_week, hpd,
              data.remark, is_active, data.default_task_id,
              data.active_from, data.active_to))
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

        # Standard-Task: das Frontend sendet dieses Feld beim Speichern des
        # Mitarbeiter-Formulars immer explizit mit (auch null, um ihn zu
        # entfernen) – daher wird der Wert direkt übernommen, statt wie bei
        # anderen optionalen Feldern nur bei "not None" zu überschreiben.
        new_default_task_id = data.default_task_id

        # active_from/active_to: analog zu default_task_id sendet das
        # Frontend diese Felder beim Speichern immer explizit mit (auch
        # null, um sie zu entfernen) – daher werden sie direkt übernommen.
        new_active_from = data.active_from
        new_active_to   = data.active_to

        # Wird ein aktiver Zeitraum gesetzt, muss is_active zwingend TRUE sein
        # – unabhängig davon, was im Formular für "Aktiv" übergeben wurde.
        if new_active_from is not None or new_active_to is not None:
            new_active = True

        # h/Tag (hours_per_day):
        # - Das Frontend sendet hours_per_day NUR, wenn der Nutzer das Feld
        #   in diesem Speichervorgang tatsächlich manuell bearbeitet hat.
        #   In diesem Fall ist eine Bemerkung (remark) Pflicht.
        # - Wurde h/Tag NICHT angefasst, aber h/Woche hat sich gegenüber dem
        #   gespeicherten Wert tatsächlich geändert, wird h/Tag automatisch
        #   neu berechnet (h/Woche / 5).
        # - Wurde weder h/Tag noch (effektiv) h/Woche geändert, bleibt der
        #   bestehende h/Tag-Wert unangetastet – so wird ein zuvor manuell
        #   gesetzter Wert beim Speichern anderer Felder (z.B. Rollen,
        #   Aktiv-Zeitraum) nicht versehentlich überschrieben und es wird
        #   auch keine unnötige Remark-Pflicht ausgelöst.
        if data.hours_per_day is not None:
            if not new_remark:
                raise HTTPException(
                    status_code=422,
                    detail="Remark ist Pflicht bei manuellem hours_per_day"
                )
            new_hpd = data.hours_per_day
        elif data.hours_per_week is not None and data.hours_per_week != existing["hours_per_week"]:
            new_hpd = round(new_hpw / Decimal("5"), 2)
        else:
            new_hpd = existing["hours_per_day"]

        cur.execute("""
            UPDATE staff
            SET hours_per_week = %s, hours_per_day = %s,
            remark = %s, is_active = %s, default_task_id = %s,
            active_from = %s, active_to = %s
            WHERE shortname = %s
        """, (new_hpw, new_hpd, new_remark, new_active, new_default_task_id,
              new_active_from, new_active_to, shortname))

        if data.roles is not None:
            _sync_roles(cur, shortname, data.roles, data.force_delete_plannings)

        # Wenn ein aktiver Zeitraum (nachträglich) gesetzt wurde, müssen
        # Abwesenheiten und Planungen, die außerhalb dieses Zeitraums liegen,
        # automatisch entfernt werden.
        deleted_absences_count = 0
        deleted_plannings_count = 0
        if new_active_from is not None or new_active_to is not None:
            deleted_absences_count, deleted_plannings_count = \
                _cleanup_out_of_window_absences_and_plannings(
                    cur, shortname, new_active_from, new_active_to
                )

        return {
            "shortname": shortname,
            "deleted_absences_count":  deleted_absences_count,
            "deleted_plannings_count": deleted_plannings_count,
        }

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