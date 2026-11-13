# Submission

## Candidate
- **Name:** Manas Bhardwaj
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~6.5 hours

## How to run
```bash
# 1. Setup virtual environment and dependencies from a clean clone
python -m venv .venv
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt

# 2. Initialize database schema and generate local encryption keys (.local/keys.json)
python -m solution.cli init

# 3. Generate synthetic sample dataset (using official generator)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest drops sequentially
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 5. Generate all five operational reports into reports/
python -m solution.cli report --out reports/

# 6. Run comprehensive automated test suite (33 tests)
pytest -v
```

## Design

The solution is architected as an idempotent, privacy-preserving ETL and reporting pipeline using Python 3.11+ and SQLite 3 in Write-Ahead Logging (`WAL`) mode. It decouples schema-driven validation, append-only persistence, cryptographic services, and derived business metric calculation.

### Database Schema & Relational Model
The database acts as the strict source of truth with foreign key constraints (`PRAGMA foreign_keys = ON;`) and indexes:
1. **`ingestion_files`**: Tracks file receipts via relative paths and SHA-256 content hashes. Guarantees file-level idempotency and crash safety across re-runs.
2. **`leads`**: Master table keyed on `(workflow_id, phone_lookup_hmac)`. Stores encrypted PII (`encrypted_phone`, `encrypted_customer_name`), the immutable `initial_batch_id`, business references (`external_ref`), and normalized metadata (`policy_expiry`, `premium_amount`, `preferred_language`).
3. **`lead_rejections`**: Audit ledger capturing every rejected row by `(workflow_id, batch_id, row_number, reason)`. Exposes zero PII.
4. **`interaction_events`**: Immutable, append-only interaction ledger keyed on unique `event_id`. Stores normalized timestamps in both UTC and IST, HMAC lookup tokens, and foreign key references to `leads(lead_id)`. Unlinked events are safely quarantined with `is_orphan = 1`.
5. **`quality_metrics`**: Persistent counters table tracking row-level validation and event quality metrics.

### PII Protection & Deterministic Lookup
- **AES-256-GCM**: Field-level authenticated encryption for `customer_name` and `phone`. Every encryption operation utilizes a fresh, cryptographically secure 96-bit random nonce (`os.urandom(12)`). Authenticated Associated Data (AAD) cryptographically binds the ciphertext to the record's immutable internal identity (`f"{workflow_id}:{field_name}:{lead_uuid}"`). This guarantees cryptographic separation between lookup tokens and encryption contexts, while remaining completely immune to reference mutations across duplicate batches.
- **HMAC-SHA256**: Deterministic phone matching token derived from the canonical phone (`+91XXXXXXXXXX`) using a dedicated 256-bit secret key distinct from the AES key. This allows rapid index lookups without decrypting the table or exposing raw phone numbers to rainbow table attacks. Cryptographic key isolation ensures that HMAC lookup tokens and AES encryption keys never share state or domains.
- Phone numbers are decrypted in-memory strictly during the final reporting step to produce the masked format (`+91******XXXX`).

### Append-Only Event Ledger & Delayed Orphan Resolution
Events can arrive before their corresponding lead CSV is uploaded. The pipeline maintains an immutable event stream:
- Incoming events without an existing lead match are written to `interaction_events` with `is_orphan = 1` and `lead_id = NULL`.
- Supervisors' corrections (`disposition.override`) arrive with a `call_id` but no `phone`. They link through sibling events sharing that `call_id`.
- Whenever new leads are ingested, an automated orphan resolution pass runs within the same transaction, resolving quarantined events and override records without rewriting original event payloads.

---

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **Decimal rounding convention (`ROUND_HALF_UP`)** | Indian financial and accounting standards mandate round half up to 2 decimal places. Used `decimal.Decimal` throughout to eliminate floating point drift. | `ROUND_HALF_EVEN` (banker's rounding) or standard Python `float`. |
| **Day-first date interpretation** | Specification states "Dates are day-first". For ambiguous formats (`02/03/2026`), day-first resolves to 2nd March 2026. | Month-first guessing or strict ISO-only rejection. |
| **Tie-breaking identical event timestamps** | If multiple disposition overrides or disposition sets have the exact same `occurred_at_utc`, break ties deterministically using `event_id DESC`. | Arbitrary SQLite insertion rowid order, which could lead to non-deterministic report generation. |
| **Current disposition fallback** | Spec states: "effective disposition of the most recent call (by initiated time) that has one". If the latest call has no disposition, look back chronologically to the previous initiated call that does. | Returning empty string if the latest call lacks a disposition. |
| **Events without `call.initiated`** | Spec specifies: "A call is counted only if its call.initiated event is present (and linked)". Telephony or disposition events for a `call_id` lacking an initiated event do not create ghost calls in reports. | Treating any event with a `call_id` as an initiated call. |
| **Local key persistence** | Keys are generated into `.local/keys.json` with git-exclusion on `init`. Allows reproducible local execution across runs without external cloud KMS dependencies. | Hardcoding encryption keys in source code (security vulnerability) or requiring environment variables on every command. |

---

## Failure modes considered

1. **Duplicate file delivery / Re-runs:** Checked via file relative path and SHA-256 hash in `ingestion_files`. Re-ingesting drops already processed exits cleanly without modifying counters or records.
2. **File content tampering after ingestion:** If a file with the same name is re-ingested with modified content, an `IngestionIntegrityError` is raised to prevent silent metric distortion.
3. **Mid-run process crash (Atomicity):** Every file and its associated metrics are processed within a dedicated SQLite transaction (`BEGIN IMMEDIATE`). A mid-ingestion termination rolls back all uncommitted state cleanly.
4. **Duplicate event deliveries across drops:** Enforced via `UNIQUE(event_id)`. Duplicates are counted in `duplicate_events_dropped` and skipped.
5. **Overrides arriving before call events:** Quarantined with `is_orphan = 1`. Sibling linking resolves the override as soon as the associated call events or leads arrive.
6. **Plaintext PII leaks in crash dumps or database files:** All schema PII is encrypted prior to SQL execution; verified by automated tests scanning raw SQLite binary bytes.
7. *Known unhandled mode:* Concurrent multi-process ingestion across separate processes writing to the same SQLite file simultaneously is constrained by SQLite database file locking (WAL mode allows concurrent readers, but serializes single writers).

---

## Testing

Comprehensive automated test suite implemented in `tests/` (33 passing tests):
- **Normalization (`test_normalization.py`):** Tested all Indian mobile formats, prefixes (+91, 91, 0), 9-digit numbers, invalid prefixes, landlines, international numbers, date formats, case-insensitive month names, impossible dates (e.g. 31/02), currency prefix variations, comma handling, and enum validation.
- **Leads Pipeline (`test_leads.py`):** Verified blank row skipping, non-blank row counting, schema-order rejection precedence, invalid required phones vs invalid optional fields, field warning accounting, duplicate lead merging, strict required-field validation for duplicates, and cryptographic integrity across reference mutations.
- **Events Ledger (`test_events.py`):** Verified malformed line handling, global duplicate dropping across drops, unknown disposition tracking after orphan resolution, equal timestamp tie-breaking, and persisted event privacy.
- **Derived Facts (`test_derived_facts.py`):** Verified call grouping, telephony failure handling, disposition override precedence, and lead bucket state priority (`yet_to_reach_out` -> `completed` -> `in_progress_telephony_failed` -> `in_progress_dialed`).
- **Security & Privacy (`test_security.py`):** Verified fresh AES-256-GCM nonces, AAD tamper detection, HMAC lookup token isolation across keys, and absence of plaintext names/phones in persisted SQLite database files.
- **Integration & Idempotency (`test_integration.py`):** Tested end-to-end sequential ingestion, re-ingestion idempotency, parent directory single-pass equivalence, distinct batch files with identical data, same-path changed-content rejection, and crash recovery with transaction rollback.

*What was not tested:* Distributed execution across network filesystems (NFS/CIFS), which is out of scope for SQLite.

---

## Results

- **Small Scale Baseline (2,000 leads, ~8,700 events):**
  - Ingestion (Drop 1 + Drop 2): 4.33s
  - Report Generation (all 5 reports): 0.34s
  - Total Pipeline Runtime: 4.70s
  - Peak Memory: 6.21 MB
  - Leads Stored: 1,892 (Database Size: 4.98 MB)
  - Automated tests (33 tests in pytest): 5.71s

- **Large Scale Performance Benchmark (~140,000 leads, ~600,000 events — Seed 42):**
  - Leads Stored: 132,903
  - Events Stored: 613,481 (Final Unresolved Orphans: 34,424)
  - Lead & Event Ingestion (Drop 1 + Drop 2): 639.45s (Drop 1: 326.47s, Drop 2: 312.98s)
  - Report Generation (all 5 reports): 32.64s
  - Total Pipeline Runtime: 672.11s (~11 minutes)
  - Peak Memory Traced: 431.48 MB
  - Database File Size on Disk: 358.36 MB
  - *Bottleneck Analysis:* Ingestion throughput averages ~960 events/second. The primary constraint is single-row SQLite indexed lookup queries (`SELECT 1 FROM interaction_events` and `SELECT lead_id FROM leads`) executing sequentially in Python (~1.8 million SQL calls). For the sub-2-minute stretch goal, grouping events into batch-prepared inserts (`executemany`) or memory-cached lead lookups would be implemented.

- **Data Reconciliation Observations:**
  - Noticeable volume of cross-batch duplicate leads (~3%) that update amounts and languages while preserving initial batch assignments.
  - Call events for rejected leads and un-uploaded numbers correctly identified and retained as orphans without corrupting client metrics.
  - Reconciled accounting: Total leads across batches (1,892) + Rejections (108) + Duplicates (57) = Exactly 2,057 total non-blank rows.

---

## If I had another day

1. **Large Scale Stream Processing:** Stream JSONL ingestion with chunked batch inserts (`executemany`) and index optimization to achieve the 140k leads / 600k events stretch goal in under 60 seconds.
2. **Pure SQL Reporting Views:** Implement the derived business fact queries as compiled SQLite SQL views or CTE aggregations rather than Python in-memory dataclass mapping.
3. **Pluggable Storage Engine:** Add an adapter interface allowing seamless switching between SQLite for local CLI use and PostgreSQL for production concurrent deployments using the provided `docker-compose.yml`.
4. **Export Schema Validation:** Add automated JSON Schema / Great Expectations validation on the generated report files to prevent regressions against client contracts.
