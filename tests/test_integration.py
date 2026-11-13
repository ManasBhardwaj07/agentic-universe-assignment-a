"""
End-to-end integration and idempotency test suite.
Verifies sequential ingestion, re-ingestion idempotency, parent directory equivalence,
and report contract compliance.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from solution.cli import main as cli_main


@pytest.fixture(scope="module")
def sample_data():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        # Generate small dataset with seed 99
        cmd = [
            sys.executable,
            "assignment-a-data/tools/generate_data.py",
            "--seed", "99",
            "--scale", "small",
            "--out", str(data_dir)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        yield data_dir


def test_sequential_and_reingest_idempotency(sample_data):
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        db_path = p / "test.db"
        keys_path = p / "keys.json"
        rep1_dir = p / "reports_1"
        rep2_dir = p / "reports_2"

        # 1. Init
        assert cli_main(["init", "--db", str(db_path), "--keys", str(keys_path)]) == 0

        # 2. Ingest Drop 1 then Drop 2
        assert cli_main(["ingest", str(sample_data / "drop_1"), "--db", str(db_path), "--keys", str(keys_path)]) == 0
        assert cli_main(["ingest", str(sample_data / "drop_2"), "--db", str(db_path), "--keys", str(keys_path)]) == 0

        # 3. Generate Reports
        assert cli_main(["report", "--out", str(rep1_dir), "--db", str(db_path), "--keys", str(keys_path)]) == 0

        # 4. Re-ingest Drop 1 and Drop 2 (Idempotency)
        assert cli_main(["ingest", str(sample_data / "drop_1"), "--db", str(db_path), "--keys", str(keys_path)]) == 0
        assert cli_main(["ingest", str(sample_data / "drop_2"), "--db", str(db_path), "--keys", str(keys_path)]) == 0

        # 5. Generate Reports Again
        assert cli_main(["report", "--out", str(rep2_dir), "--db", str(db_path), "--keys", str(keys_path)]) == 0

        # 6. Verify Byte-Identical Output Across All 5 Reports
        report_names = [
            "batch_summary.csv",
            "daily_overview.csv",
            "lead_report.csv",
            "rejections.csv",
            "data_quality.json",
        ]
        for name in report_names:
            content1 = (rep1_dir / name).read_bytes()
            content2 = (rep2_dir / name).read_bytes()
            assert content1 == content2, f"Mismatch in {name} after re-ingestion!"


def test_parent_directory_single_pass_equivalence(sample_data):
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)

        # Environment A: Sequential (drop_1 then drop_2)
        db_a = p / "a.db"
        keys_a = p / "keys_a.json"
        rep_a = p / "rep_a"
        cli_main(["init", "--db", str(db_a), "--keys", str(keys_a)])
        cli_main(["ingest", str(sample_data / "drop_1"), "--db", str(db_a), "--keys", str(keys_a)])
        cli_main(["ingest", str(sample_data / "drop_2"), "--db", str(db_a), "--keys", str(keys_a)])
        cli_main(["report", "--out", str(rep_a), "--db", str(db_a), "--keys", str(keys_a)])

        # Environment B: Single-pass parent directory ingestion (data/)
        db_b = p / "b.db"
        # Use same keys so hashes/encrypted AAD match for deterministic lead_report comparison
        keys_b = p / "keys_b.json"
        keys_b.write_bytes(keys_a.read_bytes())
        rep_b = p / "rep_b"
        cli_main(["init", "--db", str(db_b), "--keys", str(keys_b)])
        cli_main(["ingest", str(sample_data), "--db", str(db_b), "--keys", str(keys_b)])
        cli_main(["report", "--out", str(rep_b), "--db", str(db_b), "--keys", str(keys_b)])

        # Verify equivalence
        for name in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
            ca = (rep_a / name).read_bytes()
            cb = (rep_b / name).read_bytes()
            assert ca == cb, f"Mismatch in {name} between sequential and parent-directory ingestion!"


def test_distinct_batches_identical_content_processed_and_merged(test_env=None):
    """
    Specification Conformance:
    Two distinct CSV batches containing identical bytes must NOT be deduplicated away.
    They represent separate logical batches. The second batch must be processed, and its
    valid duplicate row must be merged and counted in duplicate_lead_rows_merged.
    Ensure replaying the original logical drop remains idempotent.
    """
    from solution.crypto import CryptoManager
    from solution.database import get_connection, init_db, get_metric
    from solution.leads import LeadIngestor
    from solution.schemas import SchemaRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        db_path = p / "test.db"
        init_db(db_path)
        conn = get_connection(db_path)
        crypto = CryptoManager.generate()
        registry = SchemaRegistry("assignment-a-data/schemas")
        ingestor = LeadIngestor(conn, crypto, registry)

        # File 1: batch_001.csv
        batch1 = p / "batch_001.csv"
        with open(batch1, "w", newline="", encoding="utf-8") as f:
            f.write("customer_name,phone,policy_number,premium_amount\n")
            f.write("John Doe,+919876543210,MOT/001,1500.00\n")

        # Ingest batch_001
        assert ingestor.ingest_csv(batch1, "motor_renewal", "drop_1") is True
        assert get_metric(conn, "lead_rows_total") == 1
        assert get_metric(conn, "duplicate_lead_rows_merged") == 0

        # File 2: batch_002.csv with identical content
        batch2 = p / "batch_002.csv"
        batch2.write_bytes(batch1.read_bytes())

        # Ingest batch_002: distinct batch filename, so must be processed and duplicate row merged!
        assert ingestor.ingest_csv(batch2, "motor_renewal", "drop_1") is True
        assert get_metric(conn, "lead_rows_total") == 2
        assert get_metric(conn, "duplicate_lead_rows_merged") == 1

        # Replaying original logical drop (batch_001): must remain idempotent (skipped)
        assert ingestor.ingest_csv(batch1, "motor_renewal", "drop_1") is False
        assert get_metric(conn, "lead_rows_total") == 2
        assert get_metric(conn, "duplicate_lead_rows_merged") == 1
        conn.close()


def test_idempotency_same_path_changed_content():
    from solution.crypto import CryptoManager
    from solution.database import get_connection, init_db
    from solution.leads import LeadIngestor
    from solution.schemas import SchemaRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        db_path = p / "test.db"
        init_db(db_path)
        conn = get_connection(db_path)
        crypto = CryptoManager.generate()
        registry = SchemaRegistry("assignment-a-data/schemas")
        ingestor = LeadIngestor(conn, crypto, registry)

        f_path = p / "batch_001.csv"
        with open(f_path, "w", newline="", encoding="utf-8") as f:
            f.write("customer_name,phone,policy_number\n")
            f.write("John Doe,+919876543210,MOT/001\n")

        # Initial ingest
        assert ingestor.ingest_csv(f_path, "motor_renewal", "drop_1") is True

        # Modify content at the same path
        with open(f_path, "w", newline="", encoding="utf-8") as f:
            f.write("customer_name,phone,policy_number\n")
            f.write("Jane Doe,+919876543211,MOT/002\n")

        # Re-ingest modified file at same path must raise ValueError
        with pytest.raises(ValueError, match="changed since previous ingestion"):
            ingestor.ingest_csv(f_path, "motor_renewal", "drop_1")
        conn.close()


def test_interrupted_ingestion_and_retry():
    from solution.crypto import CryptoManager
    from solution.database import get_connection, init_db, get_metric
    from solution.leads import LeadIngestor
    from solution.schemas import SchemaRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        db_path = p / "test.db"
        init_db(db_path)
        conn = get_connection(db_path)
        crypto = CryptoManager.generate()
        registry = SchemaRegistry("assignment-a-data/schemas")
        ingestor = LeadIngestor(conn, crypto, registry)

        f_path = p / "batch_001.csv"
        with open(f_path, "w", newline="", encoding="utf-8") as f:
            f.write("customer_name,phone,policy_number\n")
            f.write("John Doe,+919876543210,MOT/001\n")

        # Simulate interrupted transaction by intentionally raising an error mid-ingest
        class MockCrashException(Exception):
            pass

        try:
            with conn:
                ingestor._upsert_lead(
                    registry.get_workflow("motor_renewal"),
                    {"customer_name": "CrashUser", "policy_number": "MOT/CRASH"},
                    "+919876543210",
                    "motor_renewal",
                    "batch_001"
                )
                raise MockCrashException("Simulated power loss / SIGKILL")
        except MockCrashException:
            pass

        # Verify transaction rolled back completely
        row = conn.execute("SELECT * FROM leads WHERE external_ref = 'MOT/CRASH'").fetchone()
        assert row is None

        # Verify retry of normal file succeeds
        with conn:
            assert ingestor.ingest_csv(f_path, "motor_renewal", "drop_1") is True
        row2 = conn.execute("SELECT * FROM leads WHERE external_ref = 'MOT/001'").fetchone()
        assert row2 is not None
        conn.close()
