"""
Event ingestion and interaction ledger engine.
Handles JSONL parsing, malformed detection, deduplication, disposition validation,
phone HMAC tokenization, append-only persistence, and delayed orphan resolution.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from solution.crypto import CryptoManager
from solution.database import (
    check_file_status,
    increment_metric,
    record_file_ingestion,
)
from solution.leads import compute_file_hash
from solution.normalization import normalize_phone, parse_timestamp
from solution.schemas import SchemaRegistry


class EventIngestor:
    """Ingests JSONL event logs into the append-only interaction ledger."""

    def __init__(self, conn, crypto: CryptoManager, registry: SchemaRegistry):
        self.conn = conn
        self.crypto = crypto
        self.registry = registry

    def ingest_jsonl(self, file_path: Path, drop_name: str) -> bool:
        """
        Ingests an events JSONL file.
        Returns True if processed, False if skipped due to idempotency.
        """
        relative_path = f"{drop_name}/events/{file_path.name}"
        content_hash = compute_file_hash(file_path)

        already_ingested, content_changed = check_file_status(
            self.conn, relative_path, content_hash
        )
        if already_ingested:
            return False
        if content_changed:
            raise ValueError(
                f"File {relative_path} has changed since previous ingestion. Cannot safely re-ingest."
            )

        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    # Empty lines are skipped and NOT counted in events_total
                    continue

                # Non-empty lines are counted in events_total
                increment_metric(self.conn, "events_total", 1)

                # Precedence Step 1: Malformed check
                try:
                    event_data = json.loads(stripped)
                    if not isinstance(event_data, dict):
                        increment_metric(self.conn, "malformed_events", 1)
                        continue
                    event_id = event_data.get("event_id")
                    if not event_id or not isinstance(event_id, str):
                        increment_metric(self.conn, "malformed_events", 1)
                        continue
                except (json.JSONDecodeError, UnicodeDecodeError):
                    increment_metric(self.conn, "malformed_events", 1)
                    continue

                # Precedence Step 2: Global duplicate event_id check
                existing = self.conn.execute(
                    "SELECT 1 FROM interaction_events WHERE event_id = ?",
                    (event_id,)
                ).fetchone()
                if existing is not None:
                    increment_metric(self.conn, "duplicate_events_dropped", 1)
                    continue

                # Process valid event
                self._insert_event(event_data)

        # After finishing file, run delayed orphan resolution
        self.resolve_orphans()

        record_file_ingestion(
            self.conn, drop_name, "events", None, None, relative_path, content_hash
        )
        return True

    def _insert_event(self, data: dict) -> None:
        """Validate disposition, link to lead, and insert into interaction_events."""
        event_id = data["event_id"]
        event_type = data.get("event_type", "")
        workflow_id = data.get("workflow_id", "")
        call_id = data.get("call_id", "")
        occurred_at_raw = data.get("occurred_at", "")
        raw_phone = data.get("phone")
        raw_payload = data.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}

        # Sanitize any phone fields that might appear inside payload to protect privacy
        clean_payload = {
            k: ("[REDACTED]" if "phone" in k.lower() else v)
            for k, v in payload.items()
        }

        _, utc_iso, ist_iso = parse_timestamp(occurred_at_raw)

        # Disposition validation
        is_unknown_disp = 0
        if event_type.startswith("disposition."):
            code = payload.get("disposition_code", "")
            if not self.registry.is_valid_disposition(workflow_id, code):
                is_unknown_disp = 1
                increment_metric(self.conn, "unknown_disposition_events", 1)

        # Phone & Lead Linkage
        phone_hmac: Optional[str] = None
        lead_id: Optional[int] = None
        is_orphan = 1

        if raw_phone:
            canon_phone, valid = normalize_phone(raw_phone)
            if valid and canon_phone:
                phone_hmac = self.crypto.phone_lookup_hmac(canon_phone)
                # Lookup lead
                lead_row = self.conn.execute(
                    "SELECT lead_id FROM leads WHERE workflow_id = ? AND phone_lookup_hmac = ?",
                    (workflow_id, phone_hmac)
                ).fetchone()
                if lead_row:
                    lead_id = lead_row["lead_id"]
                    is_orphan = 0
        else:
            # Override event without phone: attempt to link through call_id
            sibling = self.conn.execute(
                "SELECT lead_id FROM interaction_events WHERE call_id = ? AND lead_id IS NOT NULL LIMIT 1",
                (call_id,)
            ).fetchone()
            if sibling:
                lead_id = sibling["lead_id"]
                is_orphan = 0

        now_iso = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO interaction_events (
                event_id, event_type, workflow_id, call_id, occurred_at_raw,
                occurred_at_utc, occurred_at_ist, phone_lookup_hmac, lead_id,
                is_orphan, is_unknown_disposition, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                workflow_id,
                call_id,
                occurred_at_raw,
                utc_iso,
                ist_iso,
                phone_hmac,
                lead_id,
                is_orphan,
                is_unknown_disp,
                json.dumps(clean_payload, ensure_ascii=False),
                now_iso,
            )
        )

    def resolve_orphans(self) -> int:
        """
        Delayed orphan resolution pass.
        Re-evaluates unlinked events against recently ingested leads and call_ids.
        Returns the number of resolved events.
        """
        # Step A: Link orphan events by (workflow_id, phone_lookup_hmac)
        cursor_a = self.conn.execute(
            """
            UPDATE interaction_events
            SET lead_id = (
                SELECT l.lead_id FROM leads l
                WHERE l.workflow_id = interaction_events.workflow_id
                  AND l.phone_lookup_hmac = interaction_events.phone_lookup_hmac
            ),
            is_orphan = 0
            WHERE is_orphan = 1
              AND phone_lookup_hmac IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM leads l
                WHERE l.workflow_id = interaction_events.workflow_id
                  AND l.phone_lookup_hmac = interaction_events.phone_lookup_hmac
              )
            """
        )
        resolved_a = cursor_a.rowcount

        # Step B: Link orphan override events via sibling events sharing the same call_id
        cursor_b = self.conn.execute(
            """
            UPDATE interaction_events
            SET lead_id = (
                SELECT sibling.lead_id FROM interaction_events sibling
                WHERE sibling.call_id = interaction_events.call_id
                  AND sibling.lead_id IS NOT NULL
                LIMIT 1
            ),
            is_orphan = 0
            WHERE is_orphan = 1
              AND EXISTS (
                SELECT 1 FROM interaction_events sibling
                WHERE sibling.call_id = interaction_events.call_id
                  AND sibling.lead_id IS NOT NULL
              )
            """
        )
        resolved_b = cursor_b.rowcount

        return resolved_a + resolved_b
