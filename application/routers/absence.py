"""
routers/absence.py – Abwesenheitsverwaltung (Tabelle: absence)
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import date, timedelta
from db import get_cursor
from routers.planning import _effective_hours_in_date_range, _build_at_hols

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

# ── Hilfsfunktion: Planungen entfernen, die durch eine Abwesenheit auf 0h fallen ──
def _delete_plannings_reduced_to_zero(cur, shortname: str,
                                      absence_from: date, absence_to: date) -> int:
    """
    Prüft alle Planungseinträge des Mitarbeiters, die sich zeitlich mit dem
    angegebenen Abwesenheitszeitraum überschneiden, und löscht davon NUR jene,
    deren effektive Stunden (unter Berücksichtigung ALLER Abwesenheiten des
    Mitarbeiters) im eigenen Start-/Enddatum-Bereich der Planung auf 0 fallen.

    D.h.:
    - Eine einzelne Wochenplanung wird nur gelöscht, wenn die Abwesenheit
      alle Arbeitstage dieser Planung abdeckt.
    - Bei zwei Teil-Planungen in derselben Woche (z.B. Mo–Mi / Do–Fr) wird
      nur die Teil-Planung gelöscht, deren Zeitraum durch die Abwesenheit
      auf 0 effektive Stunden reduziert wird.
    - Planungen mit weiterhin > 0 effektiven Stunden bleiben unverändert
      bestehen (keine Löschung, keine Kürzung).

    Gibt die Anzahl der tatsächlich gelöschten Planungseinträge zurück.
    """
    cur.execute("""
        SELECT planning_id, start_date, end_date
        FROM planning
        WHERE staff = %s
          AND start_date <= %s
          AND end_date   >= %s
    """, (shortname, absence_to, absence_from))
    candidates = cur.fetchall()

    if not candidates:
        return 0

    cur.execute("SELECT hours_per_day FROM staff WHERE shortname = %s", (shortname,))
    staff_row = cur.fetchone()
    hours_per_day = float(staff_row["hours_per_day"]) if staff_row else 0.0

    # Zeitraum abdecken, der alle betroffenen Planungen umfasst, damit die
    # Abwesenheiten und Feiertage dafür vollständig geladen werden.
    range_start = min(c["start_date"] for c in candidates)
    range_end   = max(c["end_date"]   for c in candidates)

    # Alle Abwesenheiten des Mitarbeiters in diesem Zeitraum laden – die
    # gerade angelegte/aktualisierte Abwesenheit ist zu diesem Zeitpunkt
    # bereits in der DB (INSERT/UPDATE lief vorher in derselben Transaktion).
    cur.execute("""
        SELECT shortname, absence_from, absence_to, absence_type
        FROM absence
        WHERE shortname = %s
          AND absence_to   >= %s
          AND absence_from <= %s
    """, (shortname, range_start, range_end))
    absences = [dict(a) for a in cur.fetchall()]

    at_hols = _build_at_hols(range_start, range_end)

    deleted = 0
    for c in candidates:
        effective_hours = _effective_hours_in_date_range(
            shortname, hours_per_day, c["start_date"], c["end_date"],
            absences, at_hols
        )
        if effective_hours <= 0:
            cur.execute("DELETE FROM planning WHERE planning_id = %s", (c["planning_id"],))
            deleted += 1

    return deleted

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
    """
    Neue Abwesenheit anlegen, mit Überlappungsprüfung.
    Planungseinträge des Mitarbeiters, die durch die neue Abwesenheit auf
    0 effektive Stunden reduziert werden, werden automatisch entfernt.
    """
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

        deleted_plannings_count = _delete_plannings_reduced_to_zero(
            cur, data.shortname, data.absence_from, data.absence_to
        )

        return {
            "absence_id": row["absence_id"],
            "deleted_plannings_count": deleted_plannings_count,
        }

@router.put("/{absence_id}")
def update_absence(absence_id: int, data: AbsenceUpdate):
    """
    Abwesenheit aktualisieren, mit Überlappungsprüfung.
    Planungseinträge des Mitarbeiters, die durch den (neuen) Abwesenheitszeitraum
    auf 0 effektive Stunden reduziert werden, werden automatisch entfernt.
    """
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

        deleted_plannings_count = _delete_plannings_reduced_to_zero(
            cur, existing["shortname"], new_from, new_to
        )

        return {
            "absence_id": absence_id,
            "deleted_plannings_count": deleted_plannings_count,
        }

@router.delete("/{absence_id}", status_code=204)
def delete_absence(absence_id: int):
    """Abwesenheit löschen."""
    with get_cursor(commit=True) as cur:
        cur.execute("DELETE FROM absence WHERE absence_id = %s", (absence_id,))
