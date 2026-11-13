"""
Unit and integration tests for event ingestion, orphan handling, and overrides.
"""
import json
import tempfile
from pathlib import Path

import pytest
from solution.crypto import CryptoManager
from solution.database import get_connection, init_db, get_metric
from solution.events import EventIngestor
from solution.leads import LeadIngestor
from solution.schemas import SchemaRegistry


@pytest.fixture
def test_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        db_path = tmp_p / "test.db"
        init_db(db_path)
        conn = get_connection(db_path)
        crypto = CryptoManager.generate()
        registry = SchemaRegistry("assignment-a-data/schemas")
        yield conn, crypto, registry, tmp_p
        conn.close()


def test_malformed_and_duplicate_events(test_env):
    conn, crypto, registry, tmp_p = test_env
    event_ingestor = EventIngestor(conn, crypto, registry)

    event_file = tmp_p / "events.jsonl"
    with open(event_file, "w", encoding="utf-8") as f:
        # Line 1: Malformed JSON
        f.write("{not valid json\n")
        # Line 2: Empty line (skipped, not in events_total)
        f.write("   \n")
        # Line 3: Missing event_id
        f.write('{"event_type": "call.initiated"}\n')
        # Line 4: Valid event
        f.write('{"event_id": "evt_001", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_01", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210"}\n')
        # Line 5: Duplicate event_id evt_001
        f.write('{"event_id": "evt_001", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "call_01", "occurred_at": "2026-10-01 10:01:00", "phone": "9876543210"}\n')

    event_ingestor.ingest_jsonl(event_file, "drop_1")

    assert get_metric(conn, "events_total") == 4  # 4 non-empty lines
    assert get_metric(conn, "malformed_events") == 2
    assert get_metric(conn, "duplicate_events_dropped") == 1


def test_delayed_orphan_resolution(test_env):
    conn, crypto, registry, tmp_p = test_env
    lead_ingestor = LeadIngestor(conn, crypto, registry)
    event_ingestor = EventIngestor(conn, crypto, registry)

    # Ingest event BEFORE lead arrives
    event_file = tmp_p / "events_early.jsonl"
    with open(event_file, "w", encoding="utf-8") as f:
        f.write('{"event_id": "evt_100", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_100", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210"}\n')
        f.write('{"event_id": "evt_101", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_100", "occurred_at": "2026-10-01 10:05:00", "payload": {"disposition_code": "renewed"}}\n')

    event_ingestor.ingest_jsonl(event_file, "drop_1")

    # Both events initially orphan
    orphans = conn.execute("SELECT COUNT(*) as cnt FROM interaction_events WHERE is_orphan = 1").fetchone()["cnt"]
    assert orphans == 2

    # Now ingest lead in Drop 2
    lead_file = tmp_p / "leads_drop2.csv"
    with open(lead_file, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("George,+919876543210,MOT/2026/100\n")

    lead_ingestor.ingest_csv(lead_file, "motor_renewal", "drop_2")
    event_ingestor.resolve_orphans()

    # Verify both events are resolved
    resolved = conn.execute("SELECT COUNT(*) as cnt FROM interaction_events WHERE is_orphan = 0").fetchone()["cnt"]
    assert resolved == 2
    remaining_orphans = conn.execute("SELECT COUNT(*) as cnt FROM interaction_events WHERE is_orphan = 1").fetchone()["cnt"]
    assert remaining_orphans == 0


def test_duplicate_events_across_drops(test_env):
    conn, crypto, registry, tmp_p = test_env
    event_ingestor = EventIngestor(conn, crypto, registry)

    f1 = tmp_p / "drop1_events.jsonl"
    with open(f1, "w", encoding="utf-8") as f:
        f.write('{"event_id": "evt_cross_1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210"}\n')
    event_ingestor.ingest_jsonl(f1, "drop_1")

    f2 = tmp_p / "drop2_events.jsonl"
    with open(f2, "w", encoding="utf-8") as f:
        f.write('{"event_id": "evt_cross_1", "event_type": "call.connected", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01 10:01:00", "phone": "9876543210"}\n')
        f.write('{"event_id": "evt_cross_2", "event_type": "call.ended", "workflow_id": "motor_renewal", "call_id": "c1", "occurred_at": "2026-10-01 10:02:00", "phone": "9876543210"}\n')
    event_ingestor.ingest_jsonl(f2, "drop_2")

    assert get_metric(conn, "duplicate_events_dropped") == 1
    total_in_db = conn.execute("SELECT COUNT(*) as cnt FROM interaction_events WHERE event_id = 'evt_cross_1'").fetchone()["cnt"]
    assert total_in_db == 1


def test_unknown_disposition_handling_and_orphan_resolution(test_env):
    from solution.reporting import ReportGenerator

    conn, crypto, registry, tmp_p = test_env
    lead_ingestor = LeadIngestor(conn, crypto, registry)
    event_ingestor = EventIngestor(conn, crypto, registry)

    # 1. Orphan event with unknown disposition code "MADE_UP_CODE"
    f1 = tmp_p / "orphan_unknown.jsonl"
    with open(f1, "w", encoding="utf-8") as f:
        f.write('{"event_id": "e_unk_1", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_unk", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210"}\n')
        f.write('{"event_id": "e_unk_2", "event_type": "disposition.set", "workflow_id": "motor_renewal", "call_id": "call_unk", "occurred_at": "2026-10-01 10:02:00", "phone": "9876543210", "payload": {"disposition_code": "MADE_UP_CODE"}}\n')
    event_ingestor.ingest_jsonl(f1, "drop_1")

    assert get_metric(conn, "unknown_disposition_events") == 1

    # 2. Ingest lead
    lead_file = tmp_p / "leads.csv"
    with open(lead_file, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("Helen,+919876543210,MOT/2026/500\n")
    lead_ingestor.ingest_csv(lead_file, "motor_renewal", "drop_2")
    event_ingestor.resolve_orphans()

    # Events are linked (is_orphan = 0)
    evs = conn.execute("SELECT event_id, is_orphan, is_unknown_disposition FROM interaction_events ORDER BY event_id").fetchall()
    assert evs[0]["is_orphan"] == 0
    assert evs[1]["is_orphan"] == 0
    assert evs[1]["is_unknown_disposition"] == 1  # Still unknown disposition!

    # Reports must ignore unknown disposition
    rep = ReportGenerator(conn, crypto, registry)
    out_dir = tmp_p / "rep_unk"
    rep.generate_all_reports(out_dir)

    import csv
    with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]
        # Current disposition is empty because MADE_UP_CODE was excluded!
        assert row["current_disposition"] == ""
        assert row["ever_success"] == "false"


def test_equal_timestamp_tie_breaking(test_env):
    from solution.reporting import ReportGenerator

    conn, crypto, registry, tmp_p = test_env
    lead_ingestor = LeadIngestor(conn, crypto, registry)
    event_ingestor = EventIngestor(conn, crypto, registry)

    lead_file = tmp_p / "leads.csv"
    with open(lead_file, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("Ian,+919876543210,MOT/2026/600\n")
    lead_ingestor.ingest_csv(lead_file, "motor_renewal", "drop_1")

    # 2 overrides at the exact same occurred_at timestamp:
    # evt_A has payment_link_sent (non-terminal)
    # evt_B has renewed (terminal, success)
    # Deterministic tie-break: largest event_id ("evt_B" > "evt_A") wins!
    events_file = tmp_p / "events_tie.jsonl"
    with open(events_file, "w", encoding="utf-8") as f:
        f.write('{"event_id": "evt_init", "event_type": "call.initiated", "workflow_id": "motor_renewal", "call_id": "call_tie", "occurred_at": "2026-10-01 10:00:00", "phone": "9876543210"}\n')
        f.write('{"event_id": "evt_A", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_tie", "occurred_at": "2026-10-01 10:05:00", "payload": {"disposition_code": "payment_link_sent"}}\n')
        f.write('{"event_id": "evt_B", "event_type": "disposition.override", "workflow_id": "motor_renewal", "call_id": "call_tie", "occurred_at": "2026-10-01 10:05:00", "payload": {"disposition_code": "renewed"}}\n')
    event_ingestor.ingest_jsonl(events_file, "drop_1")

    rep = ReportGenerator(conn, crypto, registry)
    out_dir = tmp_p / "rep_tie"
    rep.generate_all_reports(out_dir)

    import csv
    with open(out_dir / "lead_report.csv", "r", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]
        assert row["current_disposition"] == "renewed"
        assert row["bucket"] == "completed"
        assert row["ever_success"] == "true"


def test_persisted_event_privacy(test_env):
    conn, crypto, registry, tmp_p = test_env
    event_ingestor = EventIngestor(conn, crypto, registry)

    raw_phone = "+919876543210"
    event_file = tmp_p / "events_privacy.jsonl"
    with open(event_file, "w", encoding="utf-8") as f:
        # Event with phone at top level and adversarial phone in payload
        f.write(f'{{"event_id": "e_priv_1", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal", "call_id": "c_priv", "occurred_at": "2026-10-01 10:00:00", "phone": "{raw_phone}", "payload": {{"customer_phone": "{raw_phone}", "reason": "carrier_rejected"}}}}\n')
    event_ingestor.ingest_jsonl(event_file, "drop_1")

    # Query raw database record
    row = conn.execute("SELECT phone_lookup_hmac, payload_json FROM interaction_events WHERE event_id = 'e_priv_1'").fetchone()

    # Phone is tokenized in phone_lookup_hmac
    assert row["phone_lookup_hmac"] == crypto.phone_lookup_hmac(raw_phone)
    # Raw phone number is NOT in payload_json
    assert raw_phone not in row["payload_json"]
    assert "9876543210" not in row["payload_json"]
    assert "[REDACTED]" in row["payload_json"]
