"""
sync_jira.py – Jira-Synchronisierungs-Adapter
==============================================
Implementiert SyncAdapter für Atlassian Jira (Cloud & Server).
Registriert sich automatisch unter dem Typ "jira" in der SyncEngine.
Wird von routers/sync.py aufgerufen.
"""
from __future__ import annotations

import logging
import json
from datetime import date, datetime
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth

from db import get_cursor
from sync_engine import (
    SyncAdapter,
    SyncPreview,
    RecordDiff,
    FieldChange,
    ChangeType,
    register_adapter,
)

logger = logging.getLogger(__name__)

# ── Feldbeschreibungen für lesbare Diff-Anzeige ────────────────────────────────
FIELD_DISPLAY_NAMES: dict[str, str] = {
    "project_name":  "Projektname",
    "customer":      "Kunde",
    "target_hours":  "Soll-Stunden",
    "impl_hours":    "Impl-Stunden",
    "test_hours":    "Test-Stunden",
    "planned":       "Geplant",
    "done":          "Erledigt",
    "start_date":    "Start-Datum",
    "due_date":      "Due-Datum",
    "remarks":       "Bemerkung",
    "project_type":  "Projekt-Typ",
    "jira_id":       "Jira-ID",
}

# Felder, die beim Vergleich berücksichtigt werden
COMPARABLE_FIELDS = [
    "project_name", "customer", "target_hours", "impl_hours", "test_hours",
    "planned", "done", "start_date", "due_date", "remarks", "project_type",
]


@register_adapter("jira")
class JiraSyncAdapter(SyncAdapter):
    """
    Synchronisiert Jira-Issues mit der lokalen project-Tabelle.

    Zwei Sync-Richtungen:
    1. Bestehende Projekte mit jira_id → Issue in Jira suchen und Felder abgleichen.
    2. Neue Issues per JQL-Query finden → noch nicht in DB vorhandene anlegen.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        jira_cfg = config.get("jira", {})
        self.base_url       = jira_cfg["base_url"].rstrip("/")
        self.api_key        = jira_cfg["api_key"]
        self.email          = jira_cfg["email"]
        self.projects       = jira_cfg.get("projects", [])
        self.import_query   = jira_cfg.get("import_query", "")
        self.field_mapping: dict[str, str]   = jira_cfg.get("field_mapping", {})
        self.status_mapping: dict[str, dict] = jira_cfg.get("status_mapping", {})
        self.defaults: dict                  = jira_cfg.get("defaults", {})

        # API-Version konfigurierbar: "3" für Jira Cloud (Default), "2" für Jira Server
        self.api_version = str(jira_cfg.get("api_version", "3"))

        self._auth = HTTPBasicAuth(self.email, self.api_key)

    # ── HTTP-Hilfsmethoden ─────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/rest/api/{self.api_version}/{path.lstrip('/')}"
        logger.debug("[Jira] GET %s params=%s", url, params)
        resp = requests.get(url, auth=self._auth, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _search(self, jql: str, fields: list[str] | None = None, max_results: int = 200) -> list[dict]:
        """
        Führt eine JQL-Suche aus und gibt alle Issues zurück (paginiert).

        - API v3 (Jira Cloud): versucht zuerst POST /rest/api/3/search/jql
          mit cursor-basierter Paginierung; fällt bei 404/410 auf
          GET /rest/api/3/search zurück.
        - API v2 (Jira Server): nutzt direkt GET /rest/api/2/search
          mit offset-basierter Paginierung.
        """
        field_list = fields if fields else ["*all"]
        all_issues: list[dict] = []

        logger.info("[Jira] Starte JQL-Suche (api_version=%s): %s", self.api_version, jql)

        # ── Versuch 1: POST /search/jql (nur Jira Cloud / API v3) ────────────
        if self.api_version == "3":
            url_new = f"{self.base_url}/rest/api/3/search/jql"
            next_page_token: str | None = None
            try:
                while True:
                    body: dict = {
                        "jql":        jql,
                        "maxResults": min(max_results - len(all_issues), 100),
                        "fields":     field_list,
                    }
                    if next_page_token:
                        body["nextPageToken"] = next_page_token

                    logger.debug(
                        "[Jira] POST /search/jql body=%s",
                        {k: v for k, v in body.items() if k != "fields"},
                    )
                    resp = requests.post(url_new, auth=self._auth, json=body, timeout=15)

                    # 404/410 → neuer Endpunkt nicht verfügbar → Fallback
                    if resp.status_code in (404, 410):
                        logger.debug(
                            "[Jira] POST /search/jql nicht verfügbar (%s), nutze GET /search.",
                            resp.status_code,
                        )
                        raise requests.HTTPError(response=resp)

                    resp.raise_for_status()
                    data = resp.json()
                    issues = data.get("issues", [])
                    all_issues.extend(issues)
                    logger.info(
                        "[Jira] POST /search/jql: %d Issues abgerufen (gesamt: %d)",
                        len(issues), len(all_issues),
                    )

                    next_page_token = data.get("nextPageToken")
                    if not next_page_token or not issues or len(all_issues) >= max_results:
                        break

                logger.info("[Jira] JQL-Suche abgeschlossen: %d Issues total", len(all_issues))
                return all_issues

            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code not in (404, 410):
                    raise  # echter Fehler, nicht weiterversuchen
                all_issues = []  # Reset für Fallback

        # ── Fallback / direkter Pfad: GET /search (API v2 & v3) ───────────────
        logger.debug("[Jira] Nutze GET /rest/api/%s/search für JQL-Suche.", self.api_version)
        start_at = 0
        field_param = ",".join(field_list)
        while True:
            resp = requests.get(
                f"{self.base_url}/rest/api/{self.api_version}/search",
                auth=self._auth,
                params={
                    "jql":        jql,
                    "startAt":    start_at,
                    "maxResults": min(max_results - len(all_issues), 100),
                    "fields":     field_param,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            all_issues.extend(issues)
            logger.info(
                "[Jira] GET /search: startAt=%d, %d Issues (gesamt: %d, total: %d)",
                start_at, len(issues), len(all_issues), data.get("total", 0),
            )
            start_at += len(issues)
            if start_at >= data.get("total", 0) or not issues or len(all_issues) >= max_results:
                break

        logger.info("[Jira] Fallback-Suche abgeschlossen: %d Issues total", len(all_issues))
        return all_issues

    # ── Feld-Konvertierung ─────────────────────────────────────────────────────

    def _extract_field_value(self, issue: dict, jira_field: str) -> Any:
        """Extrahiert einen Feldwert aus einem Jira-Issue."""
        fields = issue.get("fields", {})
        value = fields.get(jira_field)

        if value is None:
            return None

        # Strukturierte Jira-Felder normalisieren
        if isinstance(value, dict):
            if "name" in value:
                return value["name"]
            if "displayName" in value:
                return value["displayName"]
            if "emailAddress" in value:
                return value["emailAddress"]

        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict) and "name" in first:
                return first["name"]

        return value

    def _map_issue_to_db(self, issue: dict) -> dict:
        """
        Konvertiert ein Jira-Issue anhand des konfigurierten Mappings
        in ein Dict mit DB-Spaltennamen.
        """
        payload: dict[str, Any] = dict(self.defaults)
        issue_key = issue.get("key", "?")

        logger.debug(
            "[Jira] Mapping Issue %s – verfügbare Felder: %s",
            issue_key, sorted(issue.get("fields", {}).keys()),
        )

        for jira_field, db_field in self.field_mapping.items():
            raw = self._extract_field_value(issue, jira_field)
            logger.debug(
                "[Jira] %s: Jira-Feld '%s' → DB-Feld '%s' = %r",
                issue_key, jira_field, db_field, raw,
            )

            if raw is None:
                logger.debug(
                    "[Jira] %s: Feld '%s' nicht vorhanden oder leer – übersprungen",
                    issue_key, jira_field,
                )
                continue

            # Sonderfeldbehandlung
            if db_field == "_jira_status":
                mapped = self.status_mapping.get(str(raw), {})
                logger.debug("[Jira] %s: Status '%s' → %s", issue_key, raw, mapped)
                payload.update(mapped)
                continue

            # Datums-Normalisierung
            if db_field in ("start_date", "due_date") and raw:
                try:
                    payload[db_field] = str(date.fromisoformat(str(raw)[:10]))
                except ValueError:
                    logger.warning(
                        "[Jira] %s: Ungültiges Datum für %s: %r", issue_key, db_field, raw,
                    )
                continue

            # Stunden: Jira liefert Sekunden (timetracking) oder direkte Zahlen
            if db_field in ("target_hours", "impl_hours", "test_hours"):
                try:
                    payload[db_field] = int(float(str(raw)))
                except (ValueError, TypeError):
                    logger.warning(
                        "[Jira] %s: Ungültiger Stundenwert für %s: %r", issue_key, db_field, raw,
                    )
                continue

            payload[db_field] = raw

        # Stunden-Konsistenz sicherstellen
        t = int(payload.get("target_hours") or 0)
        i = int(payload.get("impl_hours")   or 0)
        x = int(payload.get("test_hours")   or 0)
        if t == 0 and (i + x) > 0:
            payload["target_hours"] = i + x
        elif t > 0 and (i + x) == 0:
            payload["impl_hours"] = t
            payload["test_hours"] = 0
        elif t > 0 and (i + x) != t:
            # Differenz auf impl_hours aufschlagen
            payload["impl_hours"] = t - x

        # jira_id immer setzen
        payload["jira_id"] = issue_key

        logger.info(
            "[Jira] %s gemappt → %s",
            issue_key,
            {k: v for k, v in payload.items() if k not in ("remarks",)},
        )
        return payload

    @staticmethod
    def _values_differ(current: Any, new: Any) -> bool:
        """Vergleicht zwei Werte typsicher."""
        if current is None and new is None:
            return False
        if current is None or new is None:
            return True
        if isinstance(current, (date, datetime)):
            current = str(current)[:10]
        if isinstance(new, (date, datetime)):
            new = str(new)[:10]
        return str(current).strip() != str(new).strip()

    # ── Kern-Logik ─────────────────────────────────────────────────────────────

    def fetch_preview(self) -> SyncPreview:
        """
        1. Holt alle lokalen Projekte mit jira_id.
        2. Holt den zugehörigen Jira-Issue und berechnet Diffs.
        3. Führt die Import-Query aus und findet neue Projekte.
        Gibt SyncPreview zurück, ohne die DB zu verändern.
        """
        diffs: list[RecordDiff] = []
        processed_keys: set[str] = set()

        logger.info(
            "[Jira] Starte fetch_preview – Konfiguration: base_url=%s, email=%s, "
            "api_version=%s, projects=%s, import_query=%s",
            self.base_url, self.email, self.api_version, self.projects, self.import_query,
        )
        logger.info("[Jira] field_mapping: %s", self.field_mapping)
        logger.info("[Jira] status_mapping: %s", self.status_mapping)
        logger.info("[Jira] defaults: %s", self.defaults)

        # ── Schritt 1: Bestehende Projekte mit jira_id abgleichen ──────────────
        with get_cursor() as cur:
            cur.execute("""
                SELECT * FROM project
                WHERE jira_id IS NOT NULL AND jira_id != ''
                ORDER BY project_id
            """)
            local_projects = cur.fetchall()

        logger.info("[Jira] %d lokale Projekte mit jira_id gefunden", len(local_projects))
        for p in local_projects:
            logger.debug(
                "[Jira] Lokales Projekt: id=%d name=%s jira_id=%s",
                p["project_id"], p["project_name"], p["jira_id"],
            )

        for proj in local_projects:
            jira_key = proj["jira_id"]
            logger.info(
                "[Jira] Verarbeite bestehendes Projekt: %s (id=%d)",
                jira_key, proj["project_id"],
            )
            try:
                issue = self._get(f"issue/{jira_key}", params={"fields": "*all"})
                logger.debug(
                    "[Jira] Issue %s abgerufen: summary=%r",
                    jira_key, issue.get("fields", {}).get("summary"),
                )
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    logger.warning("[Jira] Issue %s nicht gefunden – übersprungen.", jira_key)
                    diffs.append(RecordDiff(
                        change_type=ChangeType.SKIP,
                        external_id=jira_key,
                        record_id=proj["project_id"],
                        display_name=proj["project_name"],
                    ))
                    continue
                raise

            new_data = self._map_issue_to_db(issue)
            field_changes = self._diff_fields(proj, new_data)
            processed_keys.add(jira_key)

            if field_changes:
                logger.info(
                    "[Jira] %s: %d Änderung(en) gefunden: %s",
                    jira_key, len(field_changes), [fc.field_name for fc in field_changes],
                )
                merged = {**dict(proj), **new_data}
                diffs.append(RecordDiff(
                    change_type=ChangeType.UPDATE,
                    external_id=jira_key,
                    record_id=proj["project_id"],
                    display_name=new_data.get("project_name", proj["project_name"]),
                    changes=field_changes,
                    raw_external=issue,
                    merged_payload=merged,
                ))
            else:
                logger.info("[Jira] %s: keine Änderungen – übersprungen", jira_key)
                diffs.append(RecordDiff(
                    change_type=ChangeType.SKIP,
                    external_id=jira_key,
                    record_id=proj["project_id"],
                    display_name=proj["project_name"],
                ))

        # ── Schritt 2: Neue Issues per Import-Query finden ─────────────────────
        if self.import_query:
            logger.info("[Jira] Starte Import-Query: %s", self.import_query)
            try:
                new_issues = self._search(self.import_query)
                logger.info("[Jira] Import-Query ergab %d Issues", len(new_issues))
            except requests.RequestException as exc:
                logger.error("[Jira] Fehler bei Import-Query: %s", exc)
                new_issues = []

            with get_cursor() as cur:
                cur.execute("SELECT jira_id FROM project WHERE jira_id IS NOT NULL")
                existing_keys = {r["jira_id"] for r in cur.fetchall()}

            logger.info("[Jira] Bereits in DB vorhandene Jira-Keys: %s", existing_keys)

            for issue in new_issues:
                jira_key = issue["key"]
                if jira_key in existing_keys or jira_key in processed_keys:
                    logger.debug("[Jira] %s bereits vorhanden – übersprungen", jira_key)
                    continue

                logger.info("[Jira] Neues Issue gefunden: %s", jira_key)
                new_data = self._map_issue_to_db(issue)

                # Farbe wird später beim tatsächlichen Anlegen vergeben
                diffs.append(RecordDiff(
                    change_type=ChangeType.CREATE,
                    external_id=jira_key,
                    record_id=None,
                    display_name=new_data.get("project_name", jira_key),
                    changes=[
                        FieldChange(
                            field_name=k,
                            display_name=FIELD_DISPLAY_NAMES.get(k, k),
                            current_value=None,
                            new_value=v,
                        )
                        for k, v in new_data.items()
                        if v is not None and k not in ("jira_id",)
                    ],
                    raw_external=issue,
                    merged_payload=new_data,
                ))

        logger.info(
            "[Jira] fetch_preview abgeschlossen: %d creates, %d updates, %d skipped",
            len([d for d in diffs if d.change_type == ChangeType.CREATE]),
            len([d for d in diffs if d.change_type == ChangeType.UPDATE]),
            len([d for d in diffs if d.change_type == ChangeType.SKIP]),
        )

        return SyncPreview(
            source_id=self.config.get("id", "jira"),
            source_type="jira",
            diffs=diffs,
        )

    def _diff_fields(self, current_row: dict, new_data: dict) -> list[FieldChange]:
        """Berechnet welche Felder sich geändert haben."""
        changes: list[FieldChange] = []
        for db_field in COMPARABLE_FIELDS:
            if db_field not in new_data:
                continue
            current_val = current_row.get(db_field)
            new_val = new_data[db_field]
            if self._values_differ(current_val, new_val):
                changes.append(FieldChange(
                    field_name=db_field,
                    display_name=FIELD_DISPLAY_NAMES.get(db_field, db_field),
                    current_value=current_val,
                    new_value=new_val,
                ))
        return changes

    # ── Anwenden ───────────────────────────────────────────────────────────────

    def apply(self, diffs: list[RecordDiff]) -> dict:
        """
        Wendet eine Liste von RecordDiffs (CREATE / UPDATE) auf die DB an.
        Wird nur mit den vom Nutzer bestätigten Diffs aufgerufen.
        """
        created = 0
        updated = 0
        errors: list[str] = []

        logger.info("[Jira] apply: %d Diffs werden angewendet", len(diffs))
        with get_cursor(commit=True) as cur:
            for diff in diffs:
                try:
                    if diff.change_type == ChangeType.CREATE:
                        logger.info("[Jira] CREATE: %s", diff.external_id)
                        self._apply_create(cur, diff)
                        created += 1
                    elif diff.change_type == ChangeType.UPDATE:
                        logger.info(
                            "[Jira] UPDATE: %s (id=%s)", diff.external_id, diff.record_id,
                        )
                        self._apply_update(cur, diff)
                        updated += 1
                except Exception as exc:
                    logger.exception(
                        "[Jira] Fehler beim Anwenden von %s: %s", diff.external_id, exc,
                    )
                    errors.append(f"{diff.external_id}: {exc}")

        logger.info(
            "[Jira] apply abgeschlossen: created=%d, updated=%d, errors=%d",
            created, updated, len(errors),
        )
        return {"created": created, "updated": updated, "errors": errors}

    def _next_free_color(self, cur) -> str:
        """Liefert den nächsten freien Farbcode (inline, ohne Import-Zirkel)."""
        PREDEFINED_COLORS = [
            "#4A90D9", "#E67E22", "#2ECC71", "#9B59B6", "#E74C3C",
            "#1ABC9C", "#F39C12", "#3498DB", "#D35400", "#27AE60",
            "#8E44AD", "#C0392B", "#16A085", "#F1C40F", "#2980B9",
        ]
        cur.execute("SELECT color_hexcode FROM project WHERE color_hexcode IS NOT NULL")
        used_p = {r["color_hexcode"] for r in cur.fetchall()}
        cur.execute("SELECT color_hexcode FROM tasks WHERE color_hexcode IS NOT NULL")
        used_t = {r["color_hexcode"] for r in cur.fetchall()}
        used = used_p | used_t
        for c in PREDEFINED_COLORS:
            if c not in used:
                return c
        import random
        while True:
            c = "#{:06X}".format(random.randint(0, 0xFFFFFF))
            if c not in used:
                return c

    @staticmethod
    def _sanitize(value):
        """
        Stellt sicher, dass kein dict/list-Wert an psycopg2 übergeben wird.
        Jira-Felder können verschachtelte Objekte enthalten, die beim Mapping
        nicht vollständig aufgelöst wurden (z.B. status, assignee, priority).
        """
        if value is None:
            return None
        if isinstance(value, dict):
            for key in ("name", "displayName", "emailAddress", "value", "key"):
                if key in value:
                    return str(value[key])
            return str(value)
        if isinstance(value, list):
            parts = [
                str(item.get("name", item)) if isinstance(item, dict) else str(item)
                for item in value
            ]
            return ", ".join(parts) or None
        return value

    def _sp(self, payload, field, default=None):
        """Shorthand: sanitized payload get."""
        return self._sanitize(payload.get(field, default))

    def _apply_create(self, cur, diff: RecordDiff) -> None:
        p = diff.merged_payload
        color = self._next_free_color(cur)
        cur.execute("""
            INSERT INTO project
            (project_name, customer, jira_id, target_hours, impl_hours, test_hours,
             planned, start_date, due_date, remarks, done, color_hexcode, sort_order, project_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                (SELECT COALESCE(MAX(sort_order),0)+1 FROM project),
                %s)
        """, (
            self._sp(p, "project_name") or diff.external_id,
            self._sp(p, "customer") or "",
            diff.external_id,
            int(self._sp(p, "target_hours") or 0),
            int(self._sp(p, "impl_hours")   or 0),
            int(self._sp(p, "test_hours")   or 0),
            bool(self._sp(p, "planned") if self._sp(p, "planned") is not None else True),
            self._sp(p, "start_date"),
            self._sp(p, "due_date"),
            self._sp(p, "remarks"),
            bool(self._sp(p, "done") if self._sp(p, "done") is not None else False),
            color,
            self._sp(p, "project_type") or "Project",
        ))

    def _apply_update(self, cur, diff: RecordDiff) -> None:
        p = diff.merged_payload
        cur.execute("""
            UPDATE project SET
                project_name=%s, customer=%s, target_hours=%s,
                impl_hours=%s, test_hours=%s, planned=%s,
                start_date=%s, due_date=%s, remarks=%s,
                done=%s, project_type=%s
            WHERE project_id=%s
        """, (
            self._sp(p, "project_name"),
            self._sp(p, "customer"),
            int(self._sp(p, "target_hours") or 0),
            int(self._sp(p, "impl_hours")   or 0),
            int(self._sp(p, "test_hours")   or 0),
            bool(self._sp(p, "planned") if self._sp(p, "planned") is not None else True),
            self._sp(p, "start_date"),
            self._sp(p, "due_date"),
            self._sp(p, "remarks"),
            bool(self._sp(p, "done") if self._sp(p, "done") is not None else False),
            self._sp(p, "project_type") or "Project",
            diff.record_id,
        ))