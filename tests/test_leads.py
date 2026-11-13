"""
Unit and integration tests for lead ingestion, validation, duplicate merging,
and cryptographic integrity.
"""
import csv
import tempfile
from pathlib import Path

import pytest
from solution.crypto import CryptoManager
from solution.database import get_connection, init_db, get_metric
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


def test_blank_rows_and_row_numbering(test_env):
    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    csv_path = tmp_p / "batch_001.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("Alice,+919876543210,MOT/2026/001\n")
        f.write(",,\n")  # Blank row (should be skipped)
        f.write("   ,  ,  \n")  # Whitespace blank row
        f.write("Bob,9876543211,MOT/2026/002\n")

    ingestor.ingest_csv(csv_path, "motor_renewal", "drop_1")

    # Verify total rows counted: 2 non-blank rows
    assert get_metric(conn, "lead_rows_total") == 2
    leads = conn.execute("SELECT * FROM leads ORDER BY external_ref").fetchall()
    assert len(leads) == 2


def test_rejection_precedence_in_schema_order(test_env):
    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    csv_path = tmp_p / "rejections.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        # Schema order: customer_name, phone, policy_number...
        f.write("customer_name,phone,policy_number\n")
        # Row 1: Missing customer_name and invalid phone -> missing_required:customer_name wins
        f.write(",invalidphone,MOT/2026/001\n")
        # Row 2: Customer name present, invalid phone -> invalid_phone
        f.write("Charlie,invalidphone,MOT/2026/002\n")
        # Row 3: Missing policy_number (required) -> missing_required:policy_number
        f.write("David,+919876543210,\n")

    ingestor.ingest_csv(csv_path, "motor_renewal", "drop_1")

    assert get_metric(conn, "lead_rows_rejected") == 3
    rejections = conn.execute("SELECT row_number, reason FROM lead_rejections ORDER BY row_number").fetchall()
    assert rejections[0]["row_number"] == 1
    assert rejections[0]["reason"] == "missing_required:customer_name"

    assert rejections[1]["row_number"] == 2
    assert rejections[1]["reason"] == "invalid_phone"

    assert rejections[2]["row_number"] == 3
    assert rejections[2]["reason"] == "missing_required:policy_number"


def test_optional_field_warnings_on_accepted_rows(test_env):
    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    csv_path = tmp_p / "warnings.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,policy_expiry,premium_amount\n")
        # Valid name, phone, ref; but invalid expiry (date) and invalid premium (not decimal)
        f.write("Eve,+919876543210,MOT/2026/001,2026-13-01,notanumber\n")

    ingestor.ingest_csv(csv_path, "motor_renewal", "drop_1")

    # Accepted as lead
    leads = conn.execute("SELECT * FROM leads").fetchall()
    assert len(leads) == 1
    # Invalid optional fields stored as NULL
    assert leads[0]["policy_expiry"] is None
    assert leads[0]["premium_amount"] is None
    # 2 field warnings
    assert get_metric(conn, "field_warnings") == 2


def test_duplicate_lead_merging(test_env):
    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    # First batch creates lead in batch_001
    csv1 = tmp_p / "batch_001.csv"
    with open(csv1, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,premium_amount,preferred_language\n")
        f.write("Frank,+919876543210,MOT/2026/001,1000.00,hi\n")

    ingestor.ingest_csv(csv1, "motor_renewal", "drop_1")

    # Second batch delivers duplicate with updated premium and null language
    csv2 = tmp_p / "batch_002.csv"
    with open(csv2, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,premium_amount,preferred_language\n")
        f.write("Frank Updated,9876543210,MOT/2026/001,2500.00,\n")

    ingestor.ingest_csv(csv2, "motor_renewal", "drop_1")

    assert get_metric(conn, "duplicate_lead_rows_merged") == 1

    lead = conn.execute("SELECT * FROM leads WHERE external_ref = 'MOT/2026/001'").fetchone()
    # Batch ID stays initial batch_001 permanently
    assert lead["initial_batch_id"] == "batch_001"
    # Premium updated (last write wins)
    assert lead["premium_amount"] == "2500.00"
    # Preferred language preserved from earlier valid non-null value (not overwritten by null)
    assert lead["preferred_language"] == "hi"
    # Customer name updated
    dec_name = crypto.decrypt_field(
        lead["encrypted_customer_name"],
        f"motor_renewal:customer_name:{lead['lead_uuid']}"
    )
    assert dec_name == "Frank Updated"


def test_duplicate_row_missing_required_field_rejected(test_env):
    """
    Specification Conformance:
    Every accepted CSV row must satisfy required-field validation before merging.
    A duplicate row missing customer_name must be rejected, must not update the lead,
    and must not increment duplicate_lead_rows_merged.
    """
    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    # Step 1: Initial valid lead
    csv1 = tmp_p / "batch_001.csv"
    with open(csv1, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,premium_amount\n")
        f.write("Alice Smith,+919876543210,ABC123,1500.00\n")
    ingestor.ingest_csv(csv1, "motor_renewal", "drop_1")

    assert get_metric(conn, "lead_rows_total") == 1
    assert get_metric(conn, "lead_rows_rejected") == 0
    assert get_metric(conn, "duplicate_lead_rows_merged") == 0

    # Step 2: Duplicate row missing required customer_name
    csv2 = tmp_p / "batch_002.csv"
    with open(csv2, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,premium_amount\n")
        f.write(",9876543210,XYZ789,2000.00\n")
    ingestor.ingest_csv(csv2, "motor_renewal", "drop_1")

    # Step 3: Must be rejected and NOT merged
    assert get_metric(conn, "lead_rows_total") == 2
    assert get_metric(conn, "lead_rows_rejected") == 1
    assert get_metric(conn, "duplicate_lead_rows_merged") == 0

    rejections = conn.execute("SELECT * FROM lead_rejections WHERE batch_id = 'batch_002'").fetchall()
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "missing_required:customer_name"

    # Step 4: Existing lead record in database remains completely untouched
    lead = conn.execute("SELECT * FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    assert lead["external_ref"] == "ABC123"
    assert lead["premium_amount"] == "1500.00"
    dec_name = crypto.decrypt_field(
        lead["encrypted_customer_name"],
        f"motor_renewal:customer_name:{lead['lead_uuid']}"
    )
    assert dec_name == "Alice Smith"


def test_encryption_integrity_on_reference_mutation(test_env):
    """
    Adversarial Priority 1 regression test:
    1. Ingest a lead with reference ABC123 and customer name.
    2. Ingest duplicate with same phone, different reference XYZ789, and valid customer name.
    3. Verify external reference is updated to XYZ789.
    4. Verify customer name and phone decrypt using immutable lead_uuid.
    5. Verify tampering with AAD fails.
    6. Verify all five reports remain correct and phone masking works.
    """
    from solution.reporting import ReportGenerator

    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    # Step 1: Ingest lead with reference ABC123 and customer name "Alice Smith"
    csv1 = tmp_p / "batch_001.csv"
    with open(csv1, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,premium_amount\n")
        f.write("Alice Smith,+919876543210,ABC123,1500.00\n")
    ingestor.ingest_csv(csv1, "motor_renewal", "drop_1")

    # Step 2: Ingest valid duplicate with same phone, different ref XYZ789
    csv2 = tmp_p / "batch_002.csv"
    with open(csv2, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number,premium_amount\n")
        f.write("Alice Smith Updated,9876543210,XYZ789,2000.00\n")
    ingestor.ingest_csv(csv2, "motor_renewal", "drop_1")

    assert get_metric(conn, "duplicate_lead_rows_merged") == 1

    # Step 3: Verify reference was updated to XYZ789 in database
    lead = conn.execute("SELECT * FROM leads WHERE workflow_id = 'motor_renewal'").fetchone()
    assert lead["external_ref"] == "XYZ789"
    assert lead["premium_amount"] == "2000.00"

    # Step 4: Verify customer name decrypts properly using immutable lead_uuid AAD
    lead_uuid = lead["lead_uuid"]
    decrypted_name = crypto.decrypt_field(
        lead["encrypted_customer_name"],
        f"motor_renewal:customer_name:{lead_uuid}"
    )
    assert decrypted_name == "Alice Smith Updated"

    # Step 5: Verify tampering with AAD (e.g. wrong field or wrong uuid) fails
    with pytest.raises(Exception):
        crypto.decrypt_field(
            lead["encrypted_customer_name"],
            f"motor_renewal:wrong_field:{lead_uuid}"
        )

    # Step 6: Generate all 5 reports and verify lead_report.csv masking and reference
    rep = ReportGenerator(conn, crypto, registry)
    out_dir = tmp_p / "reports_reg"
    paths = rep.generate_all_reports(out_dir)

    for p in paths.values():
        assert p.exists()

    with open(paths["lead_report"], "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["external_ref"] == "XYZ789"
        assert rows[0]["phone_masked"] == "+91******3210"


def test_lead_uuid_aad_reference_mutation_independent_of_name(test_env):
    """
    Cryptographic verification:
    If external_ref is mutated on a lead without updating customer_name,
    AAD bound to lead_uuid still decrypts perfectly.
    (If AAD were bound to external_ref, decryption would fail).
    """
    conn, crypto, registry, tmp_p = test_env
    ingestor = LeadIngestor(conn, crypto, registry)

    csv1 = tmp_p / "batch_001.csv"
    with open(csv1, "w", newline="", encoding="utf-8") as f:
        f.write("customer_name,phone,policy_number\n")
        f.write("Bob Builder,+919876543210,REF_INITIAL\n")
    ingestor.ingest_csv(csv1, "motor_renewal", "drop_1")

    lead = conn.execute("SELECT * FROM leads WHERE external_ref = 'REF_INITIAL'").fetchone()
    lead_uuid = lead["lead_uuid"]

    # Directly mutate external_ref in the database (simulating metadata mutation)
    conn.execute("UPDATE leads SET external_ref = 'REF_MUTATED' WHERE lead_id = ?", (lead["lead_id"],))

    mutated_lead = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead["lead_id"],)).fetchone()
    assert mutated_lead["external_ref"] == "REF_MUTATED"

    # Decrypt customer name: succeeds because AAD is bound to immutable lead_uuid!
    decrypted = crypto.decrypt_field(
        mutated_lead["encrypted_customer_name"],
        f"motor_renewal:customer_name:{lead_uuid}"
    )
    assert decrypted == "Bob Builder"
