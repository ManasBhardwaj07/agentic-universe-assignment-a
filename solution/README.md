# Solution: Interaction Ledger & Reporting Pipeline (Assignment A)

A production-grade, reproducible, idempotent data pipeline that ingests messy lead CSVs and call event streams into an append-only relational ledger (SQLite WAL mode), protects PII at rest via AES-256-GCM and deterministic HMAC-SHA256 tokenization, dynamically resolves delayed event linkages, and generates five operational reports strictly adhering to the output contract.

---

## 1. Architecture & Component Boundaries

The system is organized into modular components under `solution/`:

- **`cli.py`**: Command-line entry points (`init`, `ingest`, `report`) built on standard library `argparse`.
- **`crypto.py`**: Cryptographic services using `cryptography`:
  - **AES-256-GCM** authenticated field encryption for all schema fields marked `"pii": true` (`customer_name`, `phone`).
  - Fresh random 96-bit nonce per encryption operation; nonce reuse is strictly prevented.
  - Authenticated Associated Data (AAD) cryptographically binding ciphertext to the record's immutable internal identity (`f"{workflow_id}:{field_name}:{lead_uuid}"`), completely immune to reference mutations across duplicate batches.
  - **HMAC-SHA256** deterministic phone lookup token generated using a separate secret key, enabling fast joins and matching without storing plaintext phone numbers or risking dictionary attacks.
- **`schemas.py`**: Dynamic schema loader. Parses workflow definitions (`motor_renewal.json`, `policy_feedback.json`) and the disposition catalogue (`dispositions.csv`).
- **`normalization.py`**: Centralized validation and normalization rules:
  - Header alias matching (lower-cased, whitespace/underscore collapsed).
  - Indian mobile phone normalization (`+91XXXXXXXXXX`).
  - Day-first date parsing (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`).
  - Currency/decimal parsing (prefix removal `₹`, `Rs`, `Rs.`, `INR`, comma stripping, non-negative validation, `ROUND_HALF_UP` to 2 decimal places).
  - Case-insensitive enum validation.
  - ISO-8601 timestamp parsing and conversion to normalized UTC and IST.
- **`database.py`**: Relational persistence layer using SQLite 3 with Write-Ahead Logging (`WAL`), foreign key enforcement, and atomic file-level transactions.
- **`leads.py`**: Lead ingestion engine. Processes CSVs, ignores blank rows, records schema-order rejections, logs optional field warnings, merges duplicates (first batch permanent, last non-null write wins), and enforces defaults.
- **`events.py`**: Call event ingestion engine. Parses JSONL, detects malformed entries, globally deduplicates `event_id`, appends to the immutable interaction ledger, links via phone HMAC tokens or `call_id` (for overrides), and manages delayed orphan resolution.
- **`reporting.py`**: Derives business facts (initiated calls, connected calls, effective dispositions, bucket state machine) and generates deterministic reports.

---

## 2. Prerequisites & Setup

### Requirements
- Python 3.11+
- Virtual environment (`venv`)

### Installation

#### Windows (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

#### macOS / Linux (bash/zsh)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Pipeline Execution

### Step 1: Initialize Database & Cryptographic Keys
Generates SQLite schema and creates local keys at `.local/keys.json` (git-ignored):
```bash
python -m solution.cli init
```

### Step 2: Generate Synthetic Data
Generate sample data using the provided generator:
```bash
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
```

### Step 3: Ingest Data Drops
Ingest sequentially by drop, or provide the parent data folder:
```bash
# Sequential drop ingestion
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# Or single-pass parent directory ingestion
# python -m solution.cli ingest data
```

### Step 4: Generate Operational Reports
Generate the five required report artifacts into `reports/`:
```bash
python -m solution.cli report --out reports/
```

### Step 5: Verify Idempotency
Re-running ingestion on previously processed drops leaves the database and report metrics completely unchanged:
```bash
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2
python -m solution.cli report --out reports/
```

---

## 4. Automated Testing & Performance Benchmarks

### Run Automated Tests (33 Tests)
Run the full automated test suite with pytest:
```bash
pytest -v
```

### Run Performance Benchmarks
Run the reproducible pipeline benchmark on either small or large scale:
```bash
# Small scale (~2,000 leads, ~8,700 events)
python -m solution.benchmark --scale small

# Large scale (~140,000 leads, ~600,000 events)
python -m solution.benchmark --scale large
```

Test coverage includes:
- Indian mobile phone normalization across all accepted and invalid variations.
- Day-first date parsing, case-insensitive month names, and impossible date rejection.
- Currency prefix stripping, comma parsing, and decimal rounding.
- Header alias matching and unmapped column ignoring.
- Lead blank-row filtering and accurate row numbering.
- Rejection precedence in exact schema order.
- Optional field warning accounting and null assignment.
- Duplicate lead merging, permanent `initial_batch_id`, and last-write-wins non-null updates.
- Malformed JSON line handling and duplicate `event_id` dropping.
- Delayed orphan resolution across sequential drops.
- `disposition.override` linking via `call_id` and override precedence.
- Lead bucket assignment precedence (`yet_to_reach_out` -> `completed` -> `in_progress_telephony_failed` -> `in_progress_dialed`).
- Security invariants: AES-256-GCM fresh nonces, AAD tamper detection, HMAC deterministic lookup, and zero plaintext PII in database storage.
- End-to-end sequential vs parent directory ingestion equivalence.

---

## 5. Output Artifacts

Running `python -m solution.cli report --out reports/` creates:

1. **`reports/batch_summary.csv`**:
   - Headers: `workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed`
   - Sorted by: `workflow_id, batch_id`
   - Reconciles total leads to the sum of all lead bucket states.

2. **`reports/daily_overview.csv`**:
   - Headers: `workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads`
   - Sorted by: `workflow_id, date`
   - Formatted with connectivity rate to 4 decimal places.

3. **`reports/lead_report.csv`**:
   - Headers: `workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success`
   - Sorted by: `workflow_id, external_ref`
   - Displays masked phone (`+91******XXXX`), latest call initiation in ISO-8601 IST, and current effective disposition.

4. **`reports/rejections.csv`**:
   - Headers: `workflow_id,batch_id,row_number,reason`
   - Sorted by: `workflow_id, batch_id, row_number`
   - Zero PII exposure.

5. **`reports/data_quality.json`**:
   - Quality metrics capturing total lead rows, rejections by reason, merged duplicate leads, field warnings, total events, malformed lines, dropped duplicate events, final unresolved orphans, and unknown disposition events.
