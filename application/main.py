"""
Kapazitätsverwaltung – FastAPI Backend
"""

import logging
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from typing import Optional

from routers import staff, projects, tasks, planning, absence, charts, worked_hours, forecast
from routers import sync as sync_router
from routers import planning_variants
from routers import sync_config
from routers import notifications
from acl import has_permission
from scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


# ── Lifespan: Scheduler starten/stoppen ───────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startet den Hintergrund-Scheduler beim App-Start und stoppt ihn beim Beenden."""   
    try:
        start_scheduler()
    except Exception as exc:
        print("Kann scheduler nicht starten!")
        logger.error("Fehler beim Starten des Schedulers: %s", exc)
    yield
    stop_scheduler()


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Kapazitätsverwaltung", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(staff.router,             prefix="/api/staff",             tags=["Staff"])
app.include_router(projects.router,          prefix="/api/projects",          tags=["Projects"])
app.include_router(tasks.router,             prefix="/api/tasks",             tags=["Tasks"])
app.include_router(planning.router,          prefix="/api/planning",          tags=["Planning"])
app.include_router(absence.router,           prefix="/api/absence",           tags=["Absence"])
app.include_router(charts.router,            prefix="/api/charts",            tags=["Charts"])
app.include_router(worked_hours.router,      prefix="/api/worked_hours",      tags=["Worked Hours"])
app.include_router(forecast.router,          prefix="/api/forecast",          tags=["Forecast"])
app.include_router(sync_router.router,       prefix="/api/sync",              tags=["Sync"])
app.include_router(planning_variants.router, prefix="/api/planning_variants", tags=["Planning Variants"])
app.include_router(sync_config.router,         prefix="/api/sync/config",         tags=["Sync Config"])
app.include_router(notifications.router, prefix="/api/notifications",      tags=["Notifications"])


# ── Frontend HTML Seiten ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def page_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/staff", response_class=HTMLResponse)
async def page_staff(request: Request):
    editable = has_permission(request, "staff", "edit")
    return templates.TemplateResponse("staff.html", {"request": request, "has_edit_rights": editable})

@app.get("/absence", response_class=HTMLResponse)
async def page_absence(request: Request):
    editable = has_permission(request, "absence", "edit")
    return templates.TemplateResponse("absence.html", {"request": request, "has_edit_rights": editable})

@app.get("/projects", response_class=HTMLResponse)
async def page_projects(request: Request):
    editable = has_permission(request, "projects", "edit")
    return templates.TemplateResponse("projects.html", {"request": request, "has_edit_rights": editable})

@app.get("/tasks", response_class=HTMLResponse)
async def page_tasks(request: Request):
    editable = has_permission(request, "tasks", "edit")
    return templates.TemplateResponse("tasks.html", {"request": request, "has_edit_rights": editable})

@app.get("/planning", response_class=HTMLResponse)
async def page_planning(request: Request):
    editable = has_permission(request, "planning", "edit")
    return templates.TemplateResponse("planning.html", {"request": request, "has_edit_rights": editable})

@app.get("/planning_variants", response_class=HTMLResponse)
async def page_planning_variants(request: Request):
    editable = has_permission(request, "planning_variants", "edit")
    return templates.TemplateResponse("planning_variants.html", {"request": request, "has_edit_rights": editable})

@app.get("/gantt", response_class=HTMLResponse)
async def page_gantt(request: Request):
    return templates.TemplateResponse("gantt.html", {"request": request})

@app.get("/worked_hours", response_class=HTMLResponse)
async def page_worked_hours_standalone(request: Request):
    editable = has_permission(request, "worked_hours", "edit")
    return templates.TemplateResponse("worked_hours.html", {
        "request": request,
        "project_id": None,
        "has_edit_rights": editable
    })

@app.get("/worked_hours/{project_id}", response_class=HTMLResponse)
async def page_worked_hours(request: Request, project_id: int):
    editable = has_permission(request, "worked_hours", "edit")
    return templates.TemplateResponse("worked_hours.html", {
        "request": request,
        "project_id": project_id,
        "has_edit_rights": editable
    })

@app.get("/planning_status", response_class=HTMLResponse)
async def page_planning_status(
    request: Request,
    project_ids:   Optional[str] = None,
    project_names: Optional[str] = None,
    task_ids:      Optional[str] = None,
    task_names:    Optional[str] = None,
):
    editable = has_permission(request, "planning_status", "edit")
    return templates.TemplateResponse(
        "planning_status.html",
        {
            "request": request,
            "filter_project_ids":   project_ids,
            "filter_project_names": project_names,
            "filter_task_ids":      task_ids,
            "filter_task_names":    task_names,
            "has_edit_rights":      editable,
        }
    )


if __name__ == "__main__":    
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
