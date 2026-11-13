"""
Independent reconciliation test:
Verifies reports and quality metrics against raw input fixtures directly,
without relying on internal pipeline reporting functions.
"""
import csv
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest
from solution.cli import main as cli_main


def test_independent_data_reconciliation():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_p = Path(tmpdir)
        data_dir = tmp_p / "data"
        db_path = tmp_p / "audit.db"
        keys_path = tmp_p / "keys.json"
        reports_dir = tmp_p / "reports"

        # 1. Generate data with seed 42
        gen_cmd = [
            sys.executable,
            "assignment-a-data/tools/generate_data.py",
            "--seed", "42",
            "--scale", "small",
            "--out", str(data_dir)
        ]
        assert subprocess.run(gen_cmd, capture_output=True, text=True).returncode == 0

        # 2. Run pipeline
        assert cli_main(["init", "--db", str(db_path), "--keys", str(keys_path)]) == 0
        assert cli_main(["ingest", str(data_dir / "drop_1"), "--db", str(db_path), "--keys", str(keys_path)]) == 0
        assert cli_main(["ingest", str(data_dir / "drop_2"), "--db", str(db_path), "--keys", str(keys_path)]) == 0
        assert cli_main(["report", "--out", str(reports_dir), "--db", str(db_path), "--keys", str(keys_path)]) == 0

        # 3. Independent raw line counting on Leads
        lead_files = list(data_dir.glob("drop_*/leads/*/*.csv"))
        raw_nonblank_lead_rows = 0
        for lf in lead_files:
            with open(lf, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if row and any(cell.strip() for cell in row):
                        raw_nonblank_lead_rows += 1

        # 4. Independent raw line counting on Events
        event_files = list(data_dir.glob("drop_*/events/*.jsonl"))
        raw_nonempty_event_lines = 0
        for ef in event_files:
            with open(ef, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        raw_nonempty_event_lines += 1

        # 5. Load generated data_quality.json
        with open(reports_dir / "data_quality.json", "r", encoding="utf-8") as f:
            dq = json.load(f)

        assert dq["lead_rows_total"] == raw_nonblank_lead_rows
        assert dq["events_total"] == raw_nonempty_event_lines

        # Reconcile lead equation:
        # nonblank rows = unique leads + merged duplicate rows + rejected rows
        with open(reports_dir / "lead_report.csv", "r", encoding="utf-8") as f:
            lead_rows = list(csv.DictReader(f))
            unique_leads_count = len(lead_rows)

        assert (unique_leads_count + dq["duplicate_lead_rows_merged"] + dq["lead_rows_rejected"]) == raw_nonblank_lead_rows

        # 6. Verify batch_summary.csv reconciliation
        with open(reports_dir / "batch_summary.csv", "r", encoding="utf-8") as f:
            batch_rows = list(csv.DictReader(f))

        sum_batch_leads = 0
        for b in batch_rows:
            tot = int(b["total_leads"])
            yet = int(b["yet_to_reach_out"])
            tele_fail = int(b["in_progress_telephony_failed"])
            dialed = int(b["in_progress_dialed"])
            completed = int(b["completed"])
            assert tot == (yet + tele_fail + dialed + completed), f"Batch {b['batch_id']} does not balance!"
            sum_batch_leads += tot

        assert sum_batch_leads == unique_leads_count

        # 7. Verify daily_overview.csv rate calculation
        with open(reports_dir / "daily_overview.csv", "r", encoding="utf-8") as f:
            daily_rows = list(csv.DictReader(f))

        for d in daily_rows:
            tot_calls = int(d["total_calls"])
            conn_calls = int(d["connected_calls"])
            rate = float(d["connectivity_rate"])
            expected_rate = round(conn_calls / tot_calls, 4) if tot_calls > 0 else 0.0
            assert abs(rate - expected_rate) < 1e-4

        # 8. Verify rejections.csv contains no PII
        with open(reports_dir / "rejections.csv", "r", encoding="utf-8") as f:
            rej_content = f.read()
            # Ensure no 10-digit mobile phone pattern in rejections.csv
            assert not re.search(r"\b[6-9]\d{9}\b", rej_content)
            # Ensure reasons are valid specification reasons
            rej_rows = list(csv.DictReader(rej_content.splitlines()))
            for r in rej_rows:
                assert r["reason"].startswith("missing_required:") or r["reason"] == "invalid_phone"
