# routers/absence.py
"""
routers/absence.py – Abwesenheitsverwaltung (Tabelle: absence)
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import date, timedelta
from db import get_cursor
from capacity import working_days_in_range
from routers.planning import _effective_hours_in_date_range, _build_at_hols
from routers.config import get_current_fiscal_year_range

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

class TeamdayCreate(BaseModel):
    day: date

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

# ── Hilfsfunktion: Arbeitstage (Wochenenden/Feiertage raus) für eine Zeile ────
def _compute_display_days(absence_from: date, absence_to: date,
                          fiscal_year_only: bool,
                          fy_start: Optional[date], fy_end: Optional[date]):
    """
    Berechnet für eine einzelne Abwesenheit:
    - disp_from/disp_to: ggf. auf das Wirtschaftsjahr geklippter Zeitraum
    - display_calendar_days: Kalendertage in diesem (geklippten) Zeitraum
    - display_days: reine Arbeitstage (ohne Wochenenden/österr. Feiertage)
      in diesem Zeitraum, via working_days_in_range()
    """
    disp_from = absence_from
    disp_to   = absence_to
    if fiscal_year_only and fy_start is not None and fy_end is not None:
        disp_from = max(disp_from, fy_start)
        disp_to   = min(disp_to,   fy_end)

    if disp_to < disp_from:
        return disp_from, disp_to, 0, 0

    calendar_days = (disp_to - disp_from).days + 1
    working_days  = len(working_days_in_range(disp_from, disp_to))
    return disp_from, disp_to, calendar_days, working_days

# ── Endpunkte ──────────────────────────────────────────────────────────────────
@router.get("/")
def list_absences(shortname: Optional[str] = None, fiscal_year_only: bool = False):
    """
    Alle Abwesenheiten, optional gefiltert nach Mitarbeiter.

    fiscal_year_only=True: Nur Abwesenheiten, die sich mit dem Zeitraum des
    aktuellen Wirtschaftsjahres überschneiden (siehe config.json / Gruppe
    "fiscal_year" und routers/config.py). Von-/Bis-Datum werden dabei
    UNVERÄNDERT (nicht geklippt) zurückgegeben – die Einschränkung auf den
    WJ-Anteil erfolgt nur bei der Tage-Berechnung (display_days).

    Zusätzlich zu den Rohdaten liefert jede Zeile:
    - display_calendar_days: Kalendertage im (ggf. WJ-geklippten) Zeitraum
    - display_days: reine Arbeitstage (Wochenenden/Feiertage bereits
      herausgerechnet) im (ggf. WJ-geklippten) Zeitraum
    """
    fy_start: Optional[date] = None
    fy_end:   Optional[date] = None
    if fiscal_year_only:
        fy_start, fy_end = get_current_fiscal_year_range()

    with get_cursor() as cur:
        conditions = []
        params: list = []

        if shortname:
            conditions.append("shortname = %s")
            params.append(shortname)

        if fiscal_year_only:
            conditions.append("absence_from <= %s AND absence_to >= %s")
            params.extend([fy_end, fy_start])

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cur.execute(f"""
            SELECT * FROM absence
            {where}
            ORDER BY shortname, absence_from DESC
        """, params)
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        _, _, cal_days, work_days = _compute_display_days(
            r["absence_from"], r["absence_to"], fiscal_year_only, fy_start, fy_end
        )
        r["display_calendar_days"] = cal_days
        r["display_days"]          = work_days

    return rows

@router.get("/summary", response_model=List[Dict[str, Any]])
def get_absence_summary(shortname: Optional[str] = None, fiscal_year_only: bool = False):
    """
    Gibt eine Übersicht über Abwesenheitstage pro Mitarbeiter zurück.
    Umfasst Urlaubstage, GLAZ-Tage, gesamte Abwesenheitstage sowie erste und
    letzte Abwesenheit.

    Die Tage-Zählung berücksichtigt NUR Arbeitstage (Mo–Fr, keine
    österreichischen Feiertage) – Wochenenden und Feiertage werden aus der
    Zählung herausgerechnet (siehe working_days_in_range()).

    fiscal_year_only=True: Es werden nur Abwesenheiten berücksichtigt, die sich
    mit dem aktuellen Wirtschaftsjahr überschneiden (siehe config.json / Gruppe
    "fiscal_year"). Bei der Aggregation (Tage, Erste/Letzte Abwesenheit) wird
    der Zeitraum jeder Abwesenheit dabei auf das Wirtschaftsjahr geklippt,
    sodass Tage außerhalb des WJ nicht mitgezählt werden.
    """
    fy_start: Optional[date] = None
    fy_end:   Optional[date] = None
    if fiscal_year_only:
        fy_start, fy_end = get_current_fiscal_year_range()

    with get_cursor() as cur:
        conditions = []
        params: list = []
        if shortname:
            conditions.append("shortname = %s")
            params.append(shortname)
        if fiscal_year_only:
            conditions.append("absence_from <= %s AND absence_to >= %s")
            params.extend([fy_end, fy_start])
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cur.execute(f"""
            SELECT shortname, absence_from, absence_to, absence_type
            FROM absence
            {where}
            ORDER BY shortname, absence_from
        """, params)
        rows = cur.fetchall()

    summary: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        sn    = r["shortname"]
        af    = r["absence_from"]
        at    = r["absence_to"]
        atype = r["absence_type"]

        disp_from, disp_to, _, days = _compute_display_days(
            af, at, fiscal_year_only, fy_start, fy_end
        )
        if disp_to < disp_from:
            continue

        entry = summary.setdefault(sn, {
            "shortname":          sn,
            "vacation_days":      0,
            "glaz_days":          0,
            "total_absence_days": 0,
            "first_absence":      None,
            "last_absence":       None,
        })

        entry["total_absence_days"] += days
        if atype == "Urlaub":
            entry["vacation_days"] += days
        elif atype == "GLAZ":
            entry["glaz_days"] += days

        if entry["first_absence"] is None or disp_from < entry["first_absence"]:
            entry["first_absence"] = disp_from
        if entry["last_absence"] is None or disp_to > entry["last_absence"]:
            entry["last_absence"] = disp_to

    return sorted(summary.values(), key=lambda x: x["shortname"])

# ── Teamtage ───────────────────────────────────────────────────────────────────
@router.get("/teamdays")
def list_teamdays():
    """
    Alle distinct Teamtage (absence_type = 'Teamday'), inkl. Anzahl der
    Mitarbeiter, die für den jeweiligen Tag eingetragen wurden.
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT absence_from AS day, COUNT(*) AS staff_count
            FROM absence
            WHERE absence_type = 'Teamday'
            GROUP BY absence_from
            ORDER BY absence_from DESC
        """)
        return cur.fetchall()

@router.post("/teamday", status_code=201)
def create_teamday(data: TeamdayCreate):
    """
    Legt einen Teamtag an: Für alle aktiven Mitarbeiter, die an diesem Tag
    noch KEINE Abwesenheit eingetragen haben, wird eine neue Abwesenheit vom
    Typ 'Teamday' für genau diesen einen Tag angelegt. Mitarbeiter mit
    bestehender Abwesenheit an diesem Tag werden übersprungen.
    """
    day = data.day

    with get_cursor(commit=True) as cur:
        cur.execute("SELECT shortname FROM staff WHERE is_active = TRUE")
        staff_list = [r["shortname"] for r in cur.fetchall()]

        cur.execute("""
            SELECT DISTINCT shortname FROM absence
            WHERE absence_from <= %s AND absence_to >= %s
        """, (day, day))
        already_absent = {r["shortname"] for r in cur.fetchall()}

        created: List[str] = []
        skipped: List[str] = []

        for shortname in staff_list:
            if shortname in already_absent:
                skipped.append(shortname)
                continue
            cur.execute("""
                INSERT INTO absence (shortname, absence_from, absence_to, absence_type)
                VALUES (%s, %s, %s, 'Teamday')
            """, (shortname, day, day))
            created.append(shortname)

        deleted_plannings_total = 0
        for shortname in created:
            deleted_plannings_total += _delete_plannings_reduced_to_zero(cur, shortname, day, day)

        return {
            "day":                     str(day),
            "created_count":           len(created),
            "skipped_count":           len(skipped),
            "skipped_staff":           skipped,
            "deleted_plannings_count": deleted_plannings_total,
        }

@router.delete("/teamday/{day}", status_code=204)
def delete_teamday(day: date):
    """Löscht alle Teamtag-Abwesenheiten (absence_type='Teamday') für den angegebenen Tag."""
    with get_cursor(commit=True) as cur:
        cur.execute("""
            DELETE FROM absence
            WHERE absence_type = 'Teamday' AND absence_from = %s AND absence_to = %s
        """, (day, day))

# ── Standard-CRUD ────────────────────────────────────────────────────────────
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