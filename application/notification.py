"""
notification.py – E-Mail-Benachrichtigungen für Jira-Sync-Berichte
===================================================================
Erstellt strukturierte HTML-E-Mails auf Basis einer SyncPreview
und versendet sie via SMTP.

Konfiguration erfolgt über sync.json → sync_sources → <router> → <source> → notification:

  "notification": {
    "enabled": true,
    "schedule": "0 7 * * 1",          # Cron-Ausdruck (hier: Mo 07:00)
    "smtp": {
      "host":     "smtp.example.com",
      "port":     587,
      "use_tls":  true,
      "username": "noreply@example.com",
      "password": "SECRET"
    },
    "from":    "KapaBase Sync <noreply@example.com>",
    "to":      ["projektleitung@example.com"],
    "subject": "Jira-Sync Bericht – {date}",
    "only_if_changes": true            # wenn true: kein Versand bei 0 Änderungen
  }
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from sync_engine import ChangeType, SyncPreview

logger = logging.getLogger(__name__)


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

def _esc(value: Any) -> str:
    """HTML-Sonderzeichen escapen."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt(value: Any) -> str:
    """Wert für die Anzeige aufbereiten (None → '–')."""
    if value is None:
        return "–"
    return _esc(str(value))


# ── HTML-Rendering ─────────────────────────────────────────────────────────────

_BASE_STYLE = """
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #0f1117;
         color: #d8dde8; margin: 0; padding: 24px; }
  .wrap { max-width: 800px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; color: #d8dde8; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #8892aa; margin-bottom: 24px; }
  .summary { display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }
  .kpi { background: #181c27; border: 1px solid #2a3050; border-radius: 6px;
         padding: 12px 18px; min-width: 120px; }
  .kpi-label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em;
               color: #8892aa; margin-bottom: 6px; }
  .kpi-value { font-size: 22px; font-weight: 700; font-family: monospace; }
  .kpi-new    .kpi-value { color: #2ECC71; }
  .kpi-update .kpi-value { color: #E67E22; }
  .kpi-skip   .kpi-value { color: #8892aa; }
  h2 { font-size: 15px; font-weight: 600; color: #d8dde8;
       border-bottom: 1px solid #2a3050; padding-bottom: 8px; margin: 28px 0 14px; }
  .card { background: #181c27; border: 1px solid #2a3050; border-radius: 6px;
          margin-bottom: 12px; overflow: hidden; }
  .card-header { display: flex; align-items: center; gap: 10px;
                 padding: 10px 14px; background: #1e2336; }
  .card-title { font-weight: 600; font-size: 13px; }
  .card-id { font-size: 11px; color: #8892aa; font-family: monospace; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 20px;
           font-size: 10px; font-weight: 700; letter-spacing: .03em; }
  .badge-new    { background: rgba(46,204,113,.15); color: #2ECC71; }
  .badge-update { background: rgba(230,126,34,.15); color: #E67E22; }
  table.diff { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.diff th { background: #0f1117; color: #8892aa; font-size: 10px;
                  text-transform: uppercase; letter-spacing: .05em;
                  padding: 6px 10px; text-align: left;
                  border-bottom: 1px solid #2a3050; }
  table.diff td { padding: 6px 10px; border-bottom: 1px solid rgba(42,48,80,.4);
                  vertical-align: top; }
  table.diff tr:last-child td { border-bottom: none; }
  .val-field { color: #8892aa; }
  .val-old   { color: #E74C3C; text-decoration: line-through;
               font-family: monospace; font-size: 11px; }
  .val-new   { color: #2ECC71; font-family: monospace; font-size: 11px; }
  .val-create{ color: #d8dde8; font-family: monospace; font-size: 11px; }
  .no-changes { color: #8892aa; font-size: 13px; font-style: italic;
                padding: 16px 0; text-align: center; }
  .footer { margin-top: 32px; font-size: 11px; color: #8892aa;
            border-top: 1px solid #2a3050; padding-top: 12px; }
"""


def _render_html(preview: SyncPreview, run_date: date) -> str:
    """Erzeugt den vollständigen HTML-Body der Benachrichtigungs-E-Mail."""
    creates = preview.creates
    updates = preview.updates
    skipped = preview.skipped

    # ── Zusammenfassung ────────────────────────────────────────────────────────
    summary_html = f"""
<div class="summary">
  <div class="kpi kpi-new">
    <div class="kpi-label">Neue Projekte</div>
    <div class="kpi-value">{len(creates)}</div>
  </div>
  <div class="kpi kpi-update">
    <div class="kpi-label">Aktualisierungen</div>
    <div class="kpi-value">{len(updates)}</div>
  </div>
  <div class="kpi kpi-skip">
    <div class="kpi-label">Unverändert</div>
    <div class="kpi-value">{len(skipped)}</div>
  </div>
</div>"""

    # ── Neue Projekte ──────────────────────────────────────────────────────────
    create_rows = ""
    for diff in creates:
        rows = "".join(
            f"""<tr>
              <td class="val-field">{_esc(fc.display_name)}</td>
              <td class="val-create">{_fmt(fc.new_value)}</td>
            </tr>"""
            for fc in diff.changes
            if fc.new_value is not None
        )
        if not rows:
            continue
        create_rows += f"""
<div class="card">
  <div class="card-header">
    <span class="badge badge-new">Neu</span>
    <span class="card-title">{_esc(diff.display_name)}</span>
    <span class="card-id">{_esc(diff.external_id)}</span>
  </div>
  <table class="diff">
    <thead><tr><th>Feld</th><th>Wert (aus Jira)</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

    creates_section = f"""
<h2>🟢 Neue Projekte ({len(creates)})</h2>
{create_rows if create_rows else '<p class="no-changes">Keine neuen Projekte gefunden.</p>'}"""

    # ── Aktualisierungen ───────────────────────────────────────────────────────
    update_rows = ""
    for diff in updates:
        if not diff.changes:
            continue
        rows = "".join(
            f"""<tr>
              <td class="val-field">{_esc(fc.display_name)}</td>
              <td class="val-old">{_fmt(fc.current_value)}</td>
              <td class="val-new">{_fmt(fc.new_value)}</td>
            </tr>"""
            for fc in diff.changes
        )
        update_rows += f"""
<div class="card">
  <div class="card-header">
    <span class="badge badge-update">{len(diff.changes)} Änderung{"en" if len(diff.changes) != 1 else ""}</span>
    <span class="card-title">{_esc(diff.display_name)}</span>
    <span class="card-id">{_esc(diff.external_id)}</span>
  </div>
  <table class="diff">
    <thead><tr><th>Feld</th><th>Aktuell</th><th>Neu (Jira)</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""

    updates_section = f"""
<h2>🟠 Aktualisierungen ({len(updates)})</h2>
{update_rows if update_rows else '<p class="no-changes">Keine Aktualisierungen – alle verknüpften Projekte sind aktuell.</p>'}"""

    # ── Vollständiges HTML zusammenbauen ───────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<style>{_BASE_STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>Jira-Sync Bericht</h1>
  <div class="subtitle">
    Quelle: <strong>{_esc(preview.source_id)}</strong>
    &nbsp;·&nbsp;
    Datum: <strong>{run_date.strftime("%d.%m.%Y")}</strong>
  </div>
  {summary_html}
  {creates_section}
  {updates_section}
  <div class="footer">
    Automatisch generiert von KapaBase · Jira-Sync · {run_date.strftime("%d.%m.%Y")}
  </div>
</div>
</body>
</html>"""


def _render_plaintext(preview: SyncPreview, run_date: date) -> str:
    """Erzeugt einen Plain-Text-Fallback für die E-Mail."""
    lines = [
        f"Jira-Sync Bericht – {run_date.strftime('%d.%m.%Y')}",
        f"Quelle: {preview.source_id}",
        "",
        f"Neue Projekte:     {len(preview.creates)}",
        f"Aktualisierungen:  {len(preview.updates)}",
        f"Unverändert:       {len(preview.skipped)}",
        "",
    ]

    if preview.creates:
        lines.append("── NEUE PROJEKTE ──────────────────────────────────────────")
        for diff in preview.creates:
            lines.append(f"\n[Neu] {diff.display_name}  ({diff.external_id})")
            for fc in diff.changes:
                if fc.new_value is not None:
                    lines.append(f"  {fc.display_name}: {fc.new_value}")

    if preview.updates:
        lines.append("\n── AKTUALISIERUNGEN ───────────────────────────────────────")
        for diff in preview.updates:
            if not diff.changes:
                continue
            lines.append(f"\n[Update] {diff.display_name}  ({diff.external_id})")
            for fc in diff.changes:
                lines.append(
                    f"  {fc.display_name}: {fc.current_value!r}  →  {fc.new_value!r}"
                )

    return "\n".join(lines)


# ── Versand ────────────────────────────────────────────────────────────────────

def send_sync_notification(preview: SyncPreview, notif_cfg: dict) -> None:
    """
    Versendet eine HTML-E-Mail mit dem Sync-Bericht.

    :param preview:    Ergebnis des Jira-Sync-Previews.
    :param notif_cfg:  notification-Abschnitt aus sync.json für diese Quelle.
    :raises:           Propagiert SMTP-Fehler; Aufrufer entscheidet über Logging.
    """
    if not notif_cfg.get("enabled", False):
        logger.debug("[Notification] Benachrichtigung für diese Quelle deaktiviert.")
        return

    only_if_changes = notif_cfg.get("only_if_changes", True)
    if only_if_changes and not preview.creates and not preview.updates:
        logger.info(
            "[Notification] Keine Änderungen – kein E-Mail-Versand (only_if_changes=true)."
        )
        return

    today = date.today()
    subject_tpl = notif_cfg.get("subject", "Jira-Sync Bericht – {date}")
    subject     = subject_tpl.format(date=today.strftime("%d.%m.%Y"))

    html_body  = _render_html(preview, today)
    plain_body = _render_plaintext(preview, today)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = notif_cfg.get("from", "KapaBase <noreply@localhost>")
    recipients     = notif_cfg.get("to", [])
    if not recipients:
        logger.warning("[Notification] Keine Empfänger konfiguriert – Abbruch.")
        return
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    smtp_cfg  = notif_cfg.get("smtp", {})
    host      = smtp_cfg.get("host", "localhost")
    port      = int(smtp_cfg.get("port", 587))
    use_tls   = smtp_cfg.get("use_tls", True)
    username  = smtp_cfg.get("username", "")
    password  = smtp_cfg.get("password", "")

    logger.info(
        "[Notification] Versende E-Mail an %s via %s:%s (TLS=%s)",
        recipients, host, port, use_tls,
    )

    if use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls(context=context)
            if username:
                server.login(username, password)
            server.sendmail(msg["From"], recipients, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as server:
            if username:
                server.login(username, password)
            server.sendmail(msg["From"], recipients, msg.as_string())

    logger.info("[Notification] E-Mail erfolgreich versendet.")
