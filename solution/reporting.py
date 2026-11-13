"""
Reporting engine: derives business facts and generates the five required reports:
1. batch_summary.csv
2. daily_overview.csv
3. lead_report.csv
4. rejections.csv
5. data_quality.json
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from solution.crypto import CryptoManager
from solution.database import get_metric
from solution.normalization import mask_phone
from solution.schemas import SchemaRegistry


@dataclass
class CallFact:
    call_id: str
    workflow_id: str
    lead_id: int
    initiated_utc: str
    initiated_ist: str
    ist_date: str  # YYYY-MM-DD
    is_connected: bool
    is_telephony_failed: bool
    effective_disposition: Optional[str]
    is_terminal: bool
    is_success: bool


class ReportGenerator:
    """Computes derived facts and produces deterministic report artifacts."""

    def __init__(self, conn, crypto: CryptoManager, registry: SchemaRegistry):
        self.conn = conn
        self.crypto = crypto
        self.registry = registry

    def generate_all_reports(self, output_dir: Path | str) -> Dict[str, Path]:
        """Generate all five required reports into output_dir."""
        out_p = Path(output_dir)
        out_p.mkdir(parents=True, exist_ok=True)

        call_facts, lead_calls = self._compute_call_facts()

        paths = {
            "batch_summary": self._generate_batch_summary(out_p, lead_calls),
            "daily_overview": self._generate_daily_overview(out_p, call_facts),
            "lead_report": self._generate_lead_report(out_p, lead_calls),
            "rejections": self._generate_rejections(out_p),
            "data_quality": self._generate_data_quality(out_p),
        }
        return paths

    def _compute_call_facts(self) -> Tuple[List[CallFact], Dict[int, List[CallFact]]]:
        """
        Derive call facts for all initiated calls linked to leads.
        Returns: (all_call_facts, lead_to_calls_map)
        """
        # Find all valid initiated calls
        init_events = self.conn.execute(
            """
            SELECT event_id, workflow_id, call_id, lead_id, occurred_at_utc, occurred_at_ist
            FROM interaction_events
            WHERE event_type = 'call.initiated'
              AND lead_id IS NOT NULL
              AND is_orphan = 0
            ORDER BY occurred_at_utc ASC, event_id ASC
            """
        ).fetchall()

        # Group sibling events by call_id
        # We need connected, telephony_failed, disposition.set, and disposition.override
        call_events = self.conn.execute(
            """
            SELECT event_id, event_type, call_id, occurred_at_utc, payload_json, is_unknown_disposition
            FROM interaction_events
            WHERE lead_id IS NOT NULL
              AND is_orphan = 0
            ORDER BY occurred_at_utc ASC, event_id ASC
            """
        ).fetchall()

        events_by_call: Dict[str, List] = defaultdict(list)
        for ev in call_events:
            events_by_call[ev["call_id"]].append(ev)

        call_facts: List[CallFact] = []
        lead_calls: Dict[int, List[CallFact]] = defaultdict(list)

        for init_ev in init_events:
            call_id = init_ev["call_id"]
            workflow_id = init_ev["workflow_id"]
            lead_id = init_ev["lead_id"]
            init_utc = init_ev["occurred_at_utc"]
            init_ist = init_ev["occurred_at_ist"]
            ist_date = init_ist[:10]  # YYYY-MM-DD

            siblings = events_by_call.get(call_id, [])

            is_connected = any(s["event_type"] == "call.connected" for s in siblings)
            is_telephony_failed = any(s["event_type"] == "call.telephony_failed" for s in siblings)

            # Determine effective disposition
            overrides = [
                s for s in siblings
                if s["event_type"] == "disposition.override" and s["is_unknown_disposition"] == 0
            ]
            disp_sets = [
                s for s in siblings
                if s["event_type"] == "disposition.set" and s["is_unknown_disposition"] == 0
            ]

            effective_disp: Optional[str] = None
            if overrides:
                # Latest override by occurred_at_utc; tie-break largest event_id
                overrides.sort(key=lambda x: (x["occurred_at_utc"], x["event_id"]))
                latest_override = overrides[-1]
                payload = json.loads(latest_override["payload_json"])
                effective_disp = payload.get("disposition_code")
            elif disp_sets:
                # Latest disposition.set by occurred_at_utc; tie-break largest event_id
                disp_sets.sort(key=lambda x: (x["occurred_at_utc"], x["event_id"]))
                latest_disp = disp_sets[-1]
                payload = json.loads(latest_disp["payload_json"])
                effective_disp = payload.get("disposition_code")

            is_terminal = False
            is_success = False
            if effective_disp:
                disp_info = self.registry.get_disposition(workflow_id, effective_disp)
                if disp_info:
                    is_terminal = disp_info.is_terminal
                    is_success = disp_info.is_success

            fact = CallFact(
                call_id=call_id,
                workflow_id=workflow_id,
                lead_id=lead_id,
                initiated_utc=init_utc,
                initiated_ist=init_ist,
                ist_date=ist_date,
                is_connected=is_connected,
                is_telephony_failed=is_telephony_failed,
                effective_disposition=effective_disp,
                is_terminal=is_terminal,
                is_success=is_success,
            )
            call_facts.append(fact)
            lead_calls[lead_id].append(fact)

        return call_facts, lead_calls

    def _determine_bucket(self, calls: List[CallFact]) -> str:
        """
        Lead bucket evaluation order:
        1. yet_to_reach_out: no calls.
        2. completed: any call's effective disposition is terminal.
        3. in_progress_telephony_failed: every call has a call.telephony_failed event.
        4. in_progress_dialed: everything else.
        """
        if not calls:
            return "yet_to_reach_out"
        if any(c.is_terminal for c in calls):
            return "completed"
        if all(c.is_telephony_failed for c in calls):
            return "in_progress_telephony_failed"
        return "in_progress_dialed"

    def _generate_batch_summary(self, out_dir: Path, lead_calls: Dict[int, List[CallFact]]) -> Path:
        """
        1. batch_summary.csv
        workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed
        Sorted by workflow_id, batch_id
        """
        leads = self.conn.execute(
            "SELECT lead_id, workflow_id, initial_batch_id FROM leads"
        ).fetchall()

        # Group by (workflow_id, initial_batch_id)
        batch_counts = defaultdict(lambda: {
            "total_leads": 0,
            "yet_to_reach_out": 0,
            "in_progress_telephony_failed": 0,
            "in_progress_dialed": 0,
            "completed": 0,
        })

        for row in leads:
            lid = row["lead_id"]
            wf = row["workflow_id"]
            batch = row["initial_batch_id"]
            calls = lead_calls.get(lid, [])
            bucket = self._determine_bucket(calls)

            entry = batch_counts[(wf, batch)]
            entry["total_leads"] += 1
            entry[bucket] += 1

        file_path = out_dir / "batch_summary.csv"
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                "in_progress_telephony_failed", "in_progress_dialed", "completed"
            ])
            for (wf, batch), counts in sorted(batch_counts.items(), key=lambda x: (x[0][0], x[0][1])):
                writer.writerow([
                    wf,
                    batch,
                    counts["total_leads"],
                    counts["yet_to_reach_out"],
                    counts["in_progress_telephony_failed"],
                    counts["in_progress_dialed"],
                    counts["completed"],
                ])
        return file_path

    def _generate_daily_overview(self, out_dir: Path, call_facts: List[CallFact]) -> Path:
        """
        2. daily_overview.csv
        workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads
        Sorted by workflow_id, date
        """
        # Group calls by (workflow_id, ist_date)
        daily_data = defaultdict(lambda: {
            "total_calls": 0,
            "connected_calls": 0,
            "success_lead_ids": set(),
        })

        for c in call_facts:
            key = (c.workflow_id, c.ist_date)
            entry = daily_data[key]
            entry["total_calls"] += 1
            if c.is_connected:
                entry["connected_calls"] += 1
            if c.is_success:
                entry["success_lead_ids"].add(c.lead_id)

        file_path = out_dir / "daily_overview.csv"
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "date", "total_calls", "connected_calls",
                "connectivity_rate", "success_leads"
            ])
            for (wf, date_str), data in sorted(daily_data.items(), key=lambda x: (x[0][0], x[0][1])):
                tot = data["total_calls"]
                conn = data["connected_calls"]
                rate_str = f"{(conn / tot):.4f}" if tot > 0 else "0.0000"
                writer.writerow([
                    wf,
                    date_str,
                    tot,
                    conn,
                    rate_str,
                    len(data["success_lead_ids"]),
                ])
        return file_path

    def _generate_lead_report(self, out_dir: Path, lead_calls: Dict[int, List[CallFact]]) -> Path:
        """
        3. lead_report.csv
        workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success
        Sorted by workflow_id, external_ref
        """
        leads = self.conn.execute(
            """
            SELECT lead_id, workflow_id, external_ref, initial_batch_id, lead_uuid, encrypted_phone
            FROM leads
            ORDER BY workflow_id ASC, external_ref ASC
            """
        ).fetchall()

        file_path = out_dir / "lead_report.csv"
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "external_ref", "batch_id", "phone_masked",
                "bucket", "total_calls", "connected_calls", "last_call_at",
                "current_disposition", "ever_success"
            ])

            for row in leads:
                lid = row["lead_id"]
                wf = row["workflow_id"]
                ref = row["external_ref"]
                batch = row["initial_batch_id"]

                # Decrypt phone strictly for masking: +91******XXXX
                raw_phone = self.crypto.decrypt_field(
                    row["encrypted_phone"],
                    aad_context=f"{wf}:phone:{row['lead_uuid']}"
                )
                p_masked = mask_phone(raw_phone)

                calls = lead_calls.get(lid, [])
                bucket = self._determine_bucket(calls)
                total_calls = len(calls)
                connected_calls = sum(1 for c in calls if c.is_connected)

                last_call_at = ""
                current_disposition = ""
                ever_success = any(c.is_success for c in calls)

                if calls:
                    # Sort calls chronologically by initiation
                    sorted_calls = sorted(calls, key=lambda c: (c.initiated_utc, c.call_id))
                    last_call = sorted_calls[-1]
                    last_call_at = last_call.initiated_ist

                    # Current disposition: effective disposition of the most recent call
                    # (by initiated time) that has one
                    for c in reversed(sorted_calls):
                        if c.effective_disposition:
                            current_disposition = c.effective_disposition
                            break

                writer.writerow([
                    wf,
                    ref,
                    batch,
                    p_masked,
                    bucket,
                    total_calls,
                    connected_calls,
                    last_call_at,
                    current_disposition,
                    "true" if ever_success else "false",
                ])
        return file_path

    def _generate_rejections(self, out_dir: Path) -> Path:
        """
        4. rejections.csv
        workflow_id,batch_id,row_number,reason
        Sorted by workflow_id, batch_id, row_number
        Contains absolutely no PII.
        """
        rows = self.conn.execute(
            """
            SELECT workflow_id, batch_id, row_number, reason
            FROM lead_rejections
            ORDER BY workflow_id ASC, batch_id ASC, row_number ASC
            """
        ).fetchall()

        file_path = out_dir / "rejections.csv"
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
            for r in rows:
                writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])
        return file_path

    def _generate_data_quality(self, out_dir: Path) -> Path:
        """
        5. data_quality.json
        Exact schema:
        lead_rows_total, lead_rows_rejected, rejections_by_reason, duplicate_lead_rows_merged,
        field_warnings, events_total, malformed_events, duplicate_events_dropped,
        orphan_events, unknown_disposition_events
        """
        # Rejections by reason from lead_rejections table
        reasons_cursor = self.conn.execute(
            """
            SELECT reason, COUNT(*) as count
            FROM lead_rejections
            GROUP BY reason
            ORDER BY reason ASC
            """
        ).fetchall()
        rejections_by_reason = {row["reason"]: row["count"] for row in reasons_cursor}

        # Orphan events: unresolved events in final state
        orphan_count_row = self.conn.execute(
            "SELECT COUNT(*) as count FROM interaction_events WHERE is_orphan = 1"
        ).fetchone()
        orphan_events_count = orphan_count_row["count"] if orphan_count_row else 0

        quality = {
            "lead_rows_total": get_metric(self.conn, "lead_rows_total"),
            "lead_rows_rejected": get_metric(self.conn, "lead_rows_rejected"),
            "rejections_by_reason": rejections_by_reason,
            "duplicate_lead_rows_merged": get_metric(self.conn, "duplicate_lead_rows_merged"),
            "field_warnings": get_metric(self.conn, "field_warnings"),
            "events_total": get_metric(self.conn, "events_total"),
            "malformed_events": get_metric(self.conn, "malformed_events"),
            "duplicate_events_dropped": get_metric(self.conn, "duplicate_events_dropped"),
            "orphan_events": orphan_events_count,
            "unknown_disposition_events": get_metric(self.conn, "unknown_disposition_events"),
        }

        file_path = out_dir / "data_quality.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(quality, f, indent=2)
        return file_path
