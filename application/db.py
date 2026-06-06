"""
db.py – Datenbankverbindung via psycopg2 (Debian: python3-psycopg2)
Konfiguration über Umgebungsvariablen oder Standardwerte.
"""

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

# ── Verbindungsparameter (per Umgebungsvariable überschreibbar) ────────────────
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "planning")
DB_USER = os.getenv("DB_USER", "planning")
DB_PASS = os.getenv("DB_PASS", "planning")

DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"


def get_connection():
    """Gibt eine neue psycopg2-Verbindung zurück."""
    return psycopg2.connect(DSN)


@contextmanager
def get_cursor(commit: bool = False):
    """
    Context-Manager: öffnet Verbindung + DictCursor, commitet optional und
    schließt beides sauber.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
            if commit:
                conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()