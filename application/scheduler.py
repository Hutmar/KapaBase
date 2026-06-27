"""
scheduler.py – Automatisierter Jira-Sync-Benachrichtigungs-Scheduler
=====================================================================
Liest alle konfigurierten Sync-Quellen aus sync.json, registriert für
jede aktivierte Quelle mit einem notification.schedule-Cron-Ausdruck
einen APScheduler-Job und versendet den Sync-Bericht per E-Mail.

Start / Stop:
    from scheduler import start_scheduler, stop_scheduler
    start_scheduler()   # beim FastAPI-Start
    stop_scheduler()    # beim FastAPI-Stop

Der Scheduler läuft als Hintergrund-Thread und blockiert nicht den
ASGI-Event-Loop.
"""

from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from sync_engine import sync_config, get_adapter
from notification import send_sync_notification

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


# ── Job-Funktion ───────────────────────────────────────────────────────────────

def _run_sync_and_notify(router_name: str, source_id: str) -> None:
    """
    Wird vom Scheduler aufgerufen.
    1. Sync-Preview von der externen Quelle holen (kein DB-Schreiben).
    2. E-Mail versenden falls konfiguriert.
    """
    logger.info(
        "[Scheduler] Starte geplanten Sync-Lauf: router=%s  source=%s",
        router_name, source_id,
    )

    source_cfg = sync_config.get_source(router_name, source_id)
    if not source_cfg:
        logger.error(
            "[Scheduler] Quelle '%s' in Router '%s' nicht gefunden – Job übersprungen.",
            source_id, router_name,
        )
        return

    if not source_cfg.get("enabled", True):
        logger.info("[Scheduler] Quelle '%s' ist deaktiviert – übersprungen.", source_id)
        return

    notif_cfg = source_cfg.get("notification", {})
    if not notif_cfg.get("enabled", False):
        logger.info(
            "[Scheduler] Benachrichtigung für '%s' deaktiviert – übersprungen.", source_id
        )
        return

    try:
        adapter = get_adapter(source_cfg)
        preview = adapter.fetch_preview()
    except Exception as exc:
        logger.exception(
            "[Scheduler] Fehler beim Abrufen der Sync-Daten für '%s': %s",
            source_id, exc,
        )
        return

    logger.info(
        "[Scheduler] Preview abgerufen: creates=%d  updates=%d  skipped=%d",
        len(preview.creates), len(preview.updates), len(preview.skipped),
    )

    try:
        send_sync_notification(preview, notif_cfg)
    except Exception as exc:
        logger.exception(
            "[Scheduler] Fehler beim E-Mail-Versand für '%s': %s",
            source_id, exc,
        )


# ── Scheduler-Verwaltung ───────────────────────────────────────────────────────

def _parse_cron(expression: str) -> CronTrigger:
    """
    Parst einen Standard-Cron-Ausdruck (5 Felder: min h dom mon dow)
    in einen APScheduler-CronTrigger.

    Beispiele:
      "0 7 * * 1"    → jeden Montag um 07:00
      "0 8 * * 1-5"  → Mo–Fr um 08:00
      "30 6 1 * *"   → Jeden 1. des Monats um 06:30
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Ungültiger Cron-Ausdruck '{expression}'. "
            "Erwartet: 'minute hour day_of_month month day_of_week'"
        )
    minute, hour, day, month, dow = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=dow,
    )


def _register_jobs(scheduler: BackgroundScheduler) -> int:
    """
    Liest sync.json und registriert alle aktivierten Notification-Jobs.
    Gibt die Anzahl der registrierten Jobs zurück.
    """
    # Import hier, damit sync_jira den Adapter registriert
    import sync_jira  # noqa: F401

    registered = 0
    all_sources: dict = sync_config._data.get("sync_sources", {})

    for router_name, sources in all_sources.items():
        for source in sources:
            source_id  = source.get("id")
            notif_cfg  = source.get("notification", {})
            schedule   = notif_cfg.get("schedule", "")

            if not source.get("enabled", True):
                logger.debug("[Scheduler] Quelle '%s' deaktiviert – kein Job.", source_id)
                continue
            if not notif_cfg.get("enabled", False):
                logger.debug(
                    "[Scheduler] Benachrichtigung für '%s' deaktiviert – kein Job.",
                    source_id,
                )
                continue
            if not schedule:
                logger.warning(
                    "[Scheduler] Kein Cron-Schedule für '%s' konfiguriert – kein Job.",
                    source_id,
                )
                continue

            try:
                trigger = _parse_cron(schedule)
            except ValueError as exc:
                logger.error("[Scheduler] %s – Job '%s' wird nicht registriert.", exc, source_id)
                continue

            job_id = f"sync_notify__{router_name}__{source_id}"
            scheduler.add_job(
                _run_sync_and_notify,
                trigger=trigger,
                args=[router_name, source_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,  # 5 min Toleranz
            )
            logger.info(
                "[Scheduler] Job registriert: id=%s  cron='%s'",
                job_id, schedule,
            )
            registered += 1

    return registered


def start_scheduler() -> None:
    """Startet den Hintergrund-Scheduler. Idempotent."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.debug("[Scheduler] Scheduler läuft bereits.")
        return

    _scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone="Europe/Vienna",
    )

    count = _register_jobs(_scheduler)

    if count == 0:
        logger.info(
            "[Scheduler] Keine Notification-Jobs konfiguriert – "
            "Scheduler wird trotzdem gestartet (für künftige Konfiguration)."
        )

    _scheduler.start()
    logger.info("[Scheduler] Gestartet mit %d Job(s).", count)


def stop_scheduler() -> None:
    """Stoppt den Hintergrund-Scheduler. Sicher bei erneutem Aufruf."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Gestoppt.")
    _scheduler = None


def reload_scheduler() -> None:
    """
    Konfiguration neu laden und Scheduler neu starten.
    Nützlich nach Änderungen an sync.json zur Laufzeit.
    """
    sync_config.reload()
    stop_scheduler()
    start_scheduler()
    logger.info("[Scheduler] Neugeladen.")


def get_scheduled_jobs() -> list[dict]:
    """Gibt alle aktuell registrierten Jobs als Liste zurück (für API-Abfragen)."""
    if _scheduler is None or not _scheduler.running:
        return []
    return [
        {
            "id":       job.id,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
            "trigger":  str(job.trigger),
        }
        for job in _scheduler.get_jobs()
    ]
