"""
routers/charts.py – Diagramm-Endpunkte (Matplotlib, Headless/Agg)
Alle Diagramme werden serverseitig als SVG gerendert und per StreamingResponse ausgeliefert.
"""
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.ticker import MaxNLocator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from typing import Optional
from datetime import date, timedelta

from db import get_cursor
from capacity import calculate_capacity_per_week, calculate_total_capacity
from routers.forecast import ForecastResponse

router = APIRouter()


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _fig_to_svg(fig) -> StreamingResponse:
    buf = io.BytesIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/svg+xml")


def _fig_to_png(fig) -> StreamingResponse:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ── Kapazitäts-Liniendiagramm pro KW ──────────────────────────────────────────

@router.get("/capacity_per_week")
def chart_capacity_per_week(
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
):
    """Liniendiagramm: Kapazität (Stunden) pro Kalenderwoche."""
    if start_date is None:
        start_date = date.today()
    if end_date is None:
        end_date = start_date + timedelta(weeks=12)

    data   = calculate_capacity_per_week(start_date, end_date)
    weeks  = data["weeks"]
    totals = [data["totals"].get(w, 0) for w in weeks]

    fig, ax = plt.subplots(figsize=(max(8, len(weeks) * 0.5), 4))
    ax.plot(range(len(weeks)), totals, marker="o", color="#4A90D9", linewidth=2)
    ax.fill_between(range(len(weeks)), totals, alpha=0.15, color="#4A90D9")
    ax.set_xticks(range(len(weeks)))
    ax.set_xticklabels([w.replace("-W", "\nKW") for w in weeks],
                       rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Stunden")
    ax.set_title("Verfügbare Kapazität pro Kalenderwoche")
    ax.grid(axis="y", alpha=0.4)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    fig.tight_layout()
    return _fig_to_svg(fig)


# ── Burn-Down-Chart für ein einzelnes Projekt ──────────────────────────────────

@router.get("/burndown/{project_id}")
def chart_burndown(project_id: int):
    """Burn-Down-Chart: Soll-Restaufwand vs. geleistete Stunden über die Zeit."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM project WHERE project_id=%s", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return StreamingResponse(io.BytesIO(b""), media_type="image/svg+xml")

        cur.execute("""
            SELECT day, impl_hours, test_hours
            FROM worked_hours
            WHERE project_id=%s ORDER BY day ASC
        """, (project_id,))
        worked = cur.fetchall()

    target = proj["target_hours"]
    days, cumulative = [], []
    cum = 0
    for w in worked:
        cum += w["impl_hours"] + w["test_hours"]
        days.append(str(w["day"]))
        cumulative.append(target - cum)

    if days:
        steps = len(days)
        ideal = [target - (target / steps * i) for i in range(steps)]
    else:
        days       = ["Keine Daten"]
        cumulative = [target]
        ideal      = [target]

    fig, ax = plt.subplots(figsize=(max(8, len(days) * 0.5), 4))
    ax.plot(range(len(days)), ideal,      linestyle="--", color="#AAAAAA", label="Ideal")
    ax.plot(range(len(days)), cumulative, marker="o",    color="#E74C3C", linewidth=2,
            label="Restaufwand")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels(days, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Verbleibende Stunden")
    ax.set_title(f"Burn-Down: {proj['project_name']}")
    ax.legend()
    ax.grid(axis="y", alpha=0.4)
    fig.tight_layout()
    return _fig_to_svg(fig)


# ── Tortendiagramm: Kapazitätsverteilung nach Mitarbeiter ─────────────────────

@router.get("/capacity_pie")
def chart_capacity_pie(
    start_date: Optional[date] = None,
    end_date:   Optional[date] = None,
):
    if start_date is None:
        start_date = date.today()
    if end_date is None:
        end_date = start_date + timedelta(weeks=12)

    cap      = calculate_total_capacity(start_date, end_date)
    by_staff = cap["by_staff"]
    if not by_staff:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center")
        return _fig_to_svg(fig)

    labels = list(by_staff.keys())
    sizes  = list(by_staff.values())

    fig, ax = plt.subplots(figsize=(6, 6))
    ax.pie(sizes, labels=labels, autopct="%1.0f%%", startangle=90)
    ax.set_title(f"Kapazitätsverteilung\n{start_date} – {end_date}")
    fig.tight_layout()
    return _fig_to_svg(fig)


# ── Balkendiagramm: Soll vs. Ist pro Projekt ──────────────────────────────────

@router.get("/project_hours_bar")
def chart_project_hours_bar():
    """Balkendiagramm: Planstunden vs. geleistete Stunden je aktivem Projekt."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT p.project_name, p.target_hours,
                   COALESCE(SUM(wh.impl_hours + wh.test_hours), 0) AS worked
            FROM project p
            LEFT JOIN worked_hours wh ON wh.project_id = p.project_id
            WHERE p.planned = TRUE AND p.done = FALSE
            GROUP BY p.project_id, p.project_name, p.target_hours
            ORDER BY p.sort_order, p.project_name
        """)
        rows = cur.fetchall()

    if not rows:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "Keine aktiven Projekte", ha="center", va="center")
        return _fig_to_svg(fig)

    names   = [r["project_name"] for r in rows]
    targets = [r["target_hours"] for r in rows]
    worked  = [int(r["worked"]) for r in rows]
    x = range(len(names))

    fig, ax = plt.subplots(figsize=(max(6, len(names) * 1.2), 5))
    ax.bar([i - 0.2 for i in x], targets, 0.35, label="Planstunden",
           color="#4A90D9", alpha=0.85)
    ax.bar([i + 0.2 for i in x], worked,  0.35, label="Geleistet",
           color="#2ECC71", alpha=0.85)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=9)
    ax.set_ylabel("Stunden")
    ax.set_title("Planstunden vs. geleistete Stunden")
    ax.legend()
    ax.grid(axis="y", alpha=0.4)
    fig.tight_layout()
    return _fig_to_svg(fig)


# ── Forecast Burndown-Chart ────────────────────────────────────────────────────

@router.post("/forecast_burndown_chart")
def chart_forecast_burndown(forecast_data: ForecastResponse):
    """
    Rendering-Endpunkt für den Liefertermin-Forecast.
    Empfängt vorberechnete Burndown-Daten (aus /api/forecast/data)
    und liefert ein SVG-Liniendiagramm zurück.

    Trennung von Kalkulation (forecast.py) und Rendering (hier).
    """
    try:
        if not forecast_data.projects:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "Keine Forecast-Daten verfügbar", ha="center", va="center")
            return _fig_to_svg(fig)

        # X-Achse aus den tatsächlichen Burndown-Punkten des ersten Projekts ableiten.
        # Das ist robuster als forecast_data.weeks direkt zu verwenden, weil
        # die Datenpunkte (inkl. Vorwoche) und die weeks-Liste garantiert übereinstimmen.
        first_pid_str    = str(forecast_data.projects[0].project_id)
        first_bp_list    = forecast_data.burndown_data.get(first_pid_str, [])
        plot_weeks       = [bp.week_key for bp in first_bp_list]

        # Fallback, falls burndown_data leer ist
        if not plot_weeks:
            plot_weeks = forecast_data.weeks

        num_weeks = len(plot_weeks)
        if num_weeks == 0:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "Keine Wochen-Daten verfügbar", ha="center", va="center")
            return _fig_to_svg(fig)

        fig, ax = plt.subplots(figsize=(max(10, num_weeks * 0.45), 6))

        for proj in forecast_data.projects:
            pid_str          = str(proj.project_id)
            burndown_points  = forecast_data.burndown_data.get(pid_str, [])
            remaining_hours  = [bp.remaining_total for bp in burndown_points]

            # Längenprüfung: Serienlänge muss mit X-Achse übereinstimmen
            if not remaining_hours:
                continue
            if len(remaining_hours) != num_weeks:
                # Auffüllen mit 0 oder kürzen – sollte durch forecast.py nicht auftreten
                remaining_hours = remaining_hours[:num_weeks]
                while len(remaining_hours) < num_weeks:
                    remaining_hours.append(0.0)

            ax.plot(
                range(num_weeks),
                remaining_hours,
                color=proj.color_hexcode or "#555555",
                marker="o",
                markersize=4,
                linewidth=2,
                label=proj.project_name,
            )

        # X-Achse: KW-Labels (Vorwoche wird als "KW XX (Ist)" markiert)
        x_labels = []
        for i, wk in enumerate(plot_weeks):
            kw_num = int(wk.split("-W")[1])
            label  = f"KW {kw_num}" if i > 0 else f"KW {kw_num}\n(Ist)"
            x_labels.append(label)

        ax.set_xticks(range(num_weeks))
        ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("Restaufwand (Stunden)")
        ax.set_xlabel("Kalenderwoche")
        ax.set_title("Liefertermin Forecast – Burndown")
        ax.grid(axis="y", alpha=0.4)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_ylim(bottom=0)

        # Legende rechts außerhalb des Plot-Bereichs
        ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5),
                  fontsize="small", frameon=True, framealpha=0.8)

        fig.tight_layout(rect=[0, 0, 0.82, 1])
        return _fig_to_svg(fig)
    except Exception as e:
        logger.exception("Fehler beim Erstellen des Forecast Burndown Charts") # Loggt den vollständigen Traceback
        raise HTTPException(status_code=500, detail=f"Chart-Erstellung fehlgeschlagen: {e}")