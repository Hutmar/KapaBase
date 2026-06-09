@router.get("/project_status")
def project_planning_status():
    """
    Planungsstatus je aktivem Projekt:
    • geplante Stunden aus planning (hours_per_day × effektive Tage je KW)
    • abzgl. worked_hours
    • Differenz + Farbampel
    • Ist-Liefertermin (letzte KW mit Planung)
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT p.project_id, p.project_name, p.customer,
                   p.target_hours, p.impl_hours AS plan_impl,
                   p.test_hours   AS plan_test,
                   p.due_date, p.color_hexcode
            FROM project p
            WHERE p.planned = TRUE AND p.done = FALSE
            AND NOT EXISTS (
                SELECT 1 FROM tasks t WHERE t.project_id = p.project_id
            )
            ORDER BY p.due_date ASC NULLS LAST, p.project_name ASC
        """)
        projects = cur.fetchall()

        # Geleistete Stunden je Projekt
        cur.execute("""
            SELECT project_id,
                   COALESCE(SUM(impl_hours),0) AS worked_impl,
                   COALESCE(SUM(test_hours),0) AS worked_test
            FROM worked_hours GROUP BY project_id
        """)
        worked = {r["project_id"]: r for r in cur.fetchall()}

        # Planungseinträge mit Mitarbeiter-Stunden je Woche
        cur.execute("""
            SELECT pl.project_id, pl.task_id,
                   pl.staff, pl.start_date, pl.end_date,
                   r.role,
                   s.hours_per_day
            FROM planning pl
            JOIN roles r   ON r.role_id    = pl.role_id
            JOIN staff s   ON s.shortname  = pl.staff
            WHERE pl.project_id IS NOT NULL
        """)
        plan_rows = [dict(r) for r in cur.fetchall()]

        # Abwesenheiten für beteiligte Mitarbeiter
        if plan_rows:
            pstaff = list({r["staff"] for r in plan_rows})
            min_d  = min(r["start_date"] for r in plan_rows)
            max_d  = max(r["end_date"]   for r in plan_rows)
            cur.execute("""
                SELECT shortname, absence_from, absence_to
                FROM absence
                WHERE shortname = ANY(%s)
                AND absence_to >= %s AND absence_from <= %s
            """, (pstaff, min_d, max_d))
            all_absences = [dict(a) for a in cur.fetchall()]
        else:
            all_absences = []
            min_d = max_d = date.today()

        # Letztes worked_hours-Datum je Projekt
        cur.execute("""
            SELECT project_id, MAX(day) AS max_day
            FROM worked_hours GROUP BY project_id
        """)
        max_worked_day_status = {r["project_id"]: r["max_day"] for r in cur.fetchall()}

        # Feiertage
        at_hols = _build_at_hols(min_d, max_d) if plan_rows else set()

        # Geplante Stunden je Projekt + Rolle aggregieren
        # Veraltete Planungen (neuere worked_hours vorhanden) werden NICHT eingerechnet.
        plan_agg:     dict = {}   # {project_id: {'Developer': float, 'Tester': float}}
        last_end_map: dict = {}   # {project_id: date}

        for pr in plan_rows:
            pid = pr["project_id"]

            # Outdated-Check: gibt es worked_hours mit day > pl.end_date?
            is_outdated = (
                pid in max_worked_day_status
                and max_worked_day_status[pid] > pr["end_date"]
            )
            if is_outdated:
                continue  # diese Planung nicht in die offenen Stunden einrechnen

            pid  = pr["project_id"]
            wk   = iso_week_key(pr["start_date"])
            h    = _effective_hours_in_week(
                pr["staff"], float(pr["hours_per_day"]),
                wk, all_absences, at_hols)
            plan_agg.setdefault(pid, {"Developer": 0.0, "Tester": 0.0})
            role_key = pr["role"] if pr["role"] in ("Developer", "Tester") else "Developer"
            plan_agg[pid][role_key] += h

            prev = last_end_map.get(pid)
            if prev is None or pr["end_date"] > prev:
                last_end_map[pid] = pr["end_date"]

        result = []
        for p in projects:
            pid = p["project_id"]
            w   = worked.get(pid, {"worked_impl": 0, "worked_test": 0})
            pa  = plan_agg.get(pid, {"Developer": 0.0, "Tester": 0.0})

            remaining_impl = p["plan_impl"] - float(w["worked_impl"]) - pa["Developer"]
            remaining_test = p["plan_test"] - float(w["worked_test"]) - pa["Tester"]
            diff = remaining_impl + remaining_test

            # ── NEU: Restaufwand = target_hours minus tatsächlich erfasste Stunden ──
            # (unabhängig von der Planung, nur Soll vs. bereits Geleistetes)
            restaufwand = p["target_hours"] - float(w["worked_impl"]) - float(w["worked_test"])

            # ── GEÄNDERT: orange entfernt – alles ≤ 0 ist lightgreen ──
            if diff > 0:
                status_color = "red"
            elif diff == 0:
                status_color = "green"
            else:
                status_color = "lightgreen"

            # Soll-KW aus due_date
            due_kw = None
            if p["due_date"]:
                iso = p["due_date"].isocalendar()
                due_kw = f"{iso[0]}-W{iso[1]:02d}"

            # Ist-KW: letzte KW mit Planung
            last_end = last_end_map.get(pid)
            if last_end:
                iso_e = last_end.isocalendar()
                ist_kw = f"{iso_e[0]}-W{iso_e[1]:02d}"
            else:
                ist_kw = due_kw

            result.append({
                "project_id":      pid,
                "project_name":    p["project_name"],
                "customer":        p["customer"],
                "color_hexcode":   p["color_hexcode"],
                "target_hours":    p["target_hours"],
                "restaufwand":     restaufwand,        # NEU
                "due_date":        str(p["due_date"]) if p["due_date"] else None,
                "due_kw":          due_kw,
                "ist_kw":          ist_kw,
                "remaining_impl":  remaining_impl,
                "remaining_test":  remaining_test,
                "remaining_hours": diff,
                "status_color":    status_color,
            })

        return result