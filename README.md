# KapaBase 🚀

KapaBase is a modern, lightweight web application for **capacity, absence, and project management**. Specially designed for internal teams, it streamlines resource allocation (Developers and Testers) transparently on a calendar-week basis.

A key core feature is the automated capacity calculation that dynamically takes into account individual employment contracts, planned absences, and **Austrian public holidays**.

## 🛠 Technical Architecture

The application intentionally avoids heavy frontend frameworks and complex npm ecosystems. It is entirely built on a robust, server-side rendered Python architecture:

- **Backend:** FastAPI (Python 3)
- **Web Server:** Uvicorn (running natively as a systemd service)
- **Database:** PostgreSQL
- **Frontend:** HTML5, CSS3, Jinja2 Templates (Server-Side Rendering)
- **Charts:** Server-side generated via Matplotlib using the headless Agg backend (PNG/SVG outputs)

## 📦 System Requirements (Debian 13 Trixie / WSL)

All dependencies are available as native Debian packages (`.deb`). There is **no** requirement for `pip install` on the production server.

```bash
sudo apt update
sudo apt install python3-fastapi python3-uvicorn python3-psycopg2 \
                 python3-jinja2 python3-matplotlib python3-holidays \
                 postgresql
				 
