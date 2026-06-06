"""
Kapazitätsverwaltung – FastAPI Backend
Alle Abhängigkeiten als native Debian-Pakete verfügbar:
  apt install python3-fastapi python3-uvicorn python3-psycopg2
              python3-jinja2 python3-matplotlib python3-holidays
"""

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ── Sub-Router importieren ─────────────────────────────────────────────────────
from routers import staff, projects, tasks, planning, absence, charts, worked_hours

# ── App-Instanz ────────────────────────────────────────────────────────────────
app = FastAPI(title="Kapazitätsverwaltung", version="1.0.0")

# ── Statische Dateien & Templates ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Router einbinden ───────────────────────────────────────────────────────────
app.include_router(staff.router,        prefix="/api/staff",        tags=["Staff"])
app.include_router(projects.router,     prefix="/api/projects",     tags=["Projects"])
app.include_router(tasks.router,        prefix="/api/tasks",        tags=["Tasks"])
app.include_router(planning.router,     prefix="/api/planning",     tags=["Planning"])
app.include_router(absence.router,      prefix="/api/absence",      tags=["Absence"])
app.include_router(charts.router,       prefix="/api/charts",       tags=["Charts"])
app.include_router(worked_hours.router, prefix="/api/worked_hours", tags=["WorkedHours"])

# ── Frontend-Routen (HTML-Seiten) ──────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/staff", response_class=HTMLResponse)
async def page_staff(request: Request):
    return templates.TemplateResponse("staff.html", {"request": request})

@app.get("/absence", response_class=HTMLResponse)
async def page_absence(request: Request):
    return templates.TemplateResponse("absence.html", {"request": request})

@app.get("/projects", response_class=HTMLResponse)
async def page_projects(request: Request):
    return templates.TemplateResponse("projects.html", {"request": request})

@app.get("/tasks", response_class=HTMLResponse)
async def page_tasks(request: Request):
    return templates.TemplateResponse("tasks.html", {"request": request})

@app.get("/planning", response_class=HTMLResponse)
async def page_planning(request: Request):
    return templates.TemplateResponse("planning.html", {"request": request})

@app.get("/worked_hours/{project_id}", response_class=HTMLResponse)
async def page_worked_hours(request: Request, project_id: int):
    return templates.TemplateResponse("worked_hours.html",
                                      {"request": request, "project_id": project_id})

# ── Startpunkt ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
