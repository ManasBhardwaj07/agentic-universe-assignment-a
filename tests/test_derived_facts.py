"""
Unit tests for derived business facts:
Call aggregation, effective disposition resolution, override precedence, and lead bucket rules.
"""
import tempfile
from pathlib import Path

import pytest
from solution.crypto import CryptoManager
from solution.database import get_connection, init_db
from solution.events import EventIngestor
from solution.leads import LeadIngestor
from solution.reporting import ReportGenerator
from solution.schemas import SchemaRegistry


@pytest.fixture
def derived_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        db_path = tmp_p / "test.db"
        init_db(db_path)
        conn = get_connection(db_path)
        crypto = CryptoManager.generate()
        registry = SchemaRegistry("assignment-a-data/schemas")
        yield conn, crypto, registry, tmp_p
        conn.close()


def test_bucket_precedence_and_effective_disposition(derived_env):
    conn, crypto, registry, tmp_p = derived_env
    lead_ingestor = LeadIngestor(conn, crypto, registry)
    event_ingestor = EventIngestor(conn, crypto, registry)

    # Create 4 leads in batch_001
    leads_file = tmp_p / "leads.csv"
    with open(leads_file, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("Lead 1,+919876543201,MOT/001\n")  # will have 0 calls -> yet_to_reach_out
        f.write("Lead 2,+919876543202,MOT/002\n")  # will have terminal call -> completed
        f.write("Lead 3,+919876543203,MOT/003\n")  # will have only telephony_failed -> in_progress_telephony_failed
        f.write("Lead 4,+919876543204,MOT/004\n")  # will have non-terminal connected -> in_progress_dialed

    lead_ingestor.ingest_csv(leads_file, "motor_renewal", "drop_1")

    # Events
    events_file = tmp_p / "events.jsonl"
    with open(events_file, "w", encoding="utf-8") as f:
        # Lead 2: Call 1 has payment_link_sent (non-terminal), but overridden to renewed (terminal)
        f.write('{"event_id": "e01", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c02", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543202"}\n')
        f.write('{"event_id": "e02", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c02", "occurred_at": "2026-10-01 10:01:00", "phone": "9876543202"}\n')
        f.write('{"event_id": "e03", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c02", "occurred_at": "2026-10-01 10:02:00", "phone": "9876543202", "payload": {"disposition_code": "payment_link_sent"}}\n')
        f.write('{"event_id": "e04", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "c02", "occurred_at": "2026-10-01 11:00:00", "payload": {"disposition_code": "renewed"}}\n')

        # Lead 3: 2 calls, both telephony_failed
        f.write('{"event_id": "e05", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c03_1", "occurred_at": "2026-10-01 12:00:00", "phone": "9876543203"}\n')
        f.write('{"event_id": "e06", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c03_1", "occurred_at": "2026-10-01 12:00:05", "phone": "9876543203"}\n')
        f.write('{"event_id": "e07", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c03_2", "occurred_at": "2026-10-01 13:00:00", "phone": "9876543203"}\n')
        f.write('{"event_id": "e08", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c03_2", "occurred_at": "2026-10-01 13:00:05", "phone": "9876543203"}\n')

        # Lead 4: 1 call telephony_failed, 1 call connected with non-terminal disposition
        f.write('{"event_id": "e09", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c04_1", "occurred_at": "2026-10-01 14:00:00", "phone": "9876543204"}\n')
        f.write('{"event_id": "e10", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c04_1", "occurred_at": "2026-10-01 14:00:05", "phone": "9876543204"}\n')
        f.write('{"event_id": "e11", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c04_2", "occurred_at": "2026-10-01 15:00:00", "phone": "9876543204"}\n')
        f.write('{"event_id": "e12", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c04_2", "occurred_at": "2026-10-01 15:01:00", "phone": "9876543204"}\n')
        f.write('{"event_id": "e13", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "c04_2", "occurred_at": "2026-10-01 15:02:00", "phone": "9876543204", "payload": {"disposition_code": "callback_requested"}}\n')

    event_ingestor.ingest_jsonl(events_file, "drop_1")

    # Generate lead report
    rep = ReportGenerator(conn, crypto, registry)
    out_dir = tmp_p / "reports"
    rep.generate_all_reports(out_dir)

    # Read lead_report.csv
    import csv
    with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        rows = {r["external_ref"]: r for r in csv.DictReader(f)}

    # Lead 1: 0 calls -> yet_to_reach_out
    assert rows["MOT/001"]["bucket"] == "yet_to_reach_out"
    assert rows["MOT/001"]["total_calls"] == "0"
    assert rows["MOT/001"]["ever_success"] == "false"

    # Lead 2: terminal override renewed -> completed
    assert rows["MOT/002"]["bucket"] == "completed"
    assert rows["MOT/002"]["current_disposition"] == "renewed"
    assert rows["MOT/002"]["ever_success"] == "true"

    # Lead 3: only telephony_failed -> in_progress_telephony_failed
    assert rows["MOT/003"]["bucket"] == "in_progress_telephony_failed"
    assert rows["MOT/003"]["total_calls"] == "2"
    assert rows["MOT/003"]["connected_calls"] == "0"

    # Lead 4: mixed telephony_failed and non-terminal connected -> in_progress_dialed
    assert rows["MOT/004"]["bucket"] == "in_progress_dialed"
    assert rows["MOT/004"]["total_calls"] == "2"
    assert rows["MOT/004"]["connected_calls"] == "1"
    assert rows["MOT/004"]["current_disposition"] == "callback_requested"
    assert rows["MOT/004"]["ever_success"] == "false"
