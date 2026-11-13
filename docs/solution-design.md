# Architecture & Solution Design: Interaction Ledger & Reporting Pipeline (Assignment A)

## 1. Executive Summary & Problem Scope

Agentic Universe conducts outbound AI voice campaigns across workflows such as `motor_renewal` and `policy_feedback`. Client trust hinges upon absolute precision in morning operational and performance metrics (leads dialed, connectivity rates, successful dispositions, and lead state distribution).

Data arrives in discrete drops containing:
1. **Lead Uploads (CSV):** Messy tabular data with varying headers, aliases, uncounted blank rows, mixed date formats, currency prefixes, invalid values, duplicates across batches, and PII requiring strict protection at rest.
2. **Call Events (JSONL):** Append-only interaction events (`call.initiated`, `call.telephony_failed`, `call.connected`, `call.ended`, `disposition.set`, `disposition.override`) with mixed timestamps (UTC, offset IST, naive IST), duplicates across drops, malformed entries, out-of-order deliveries, and events that arrive before their corresponding lead is ingested.

The solution must ingest these streams idempotently, maintain an append-only interaction ledger, protect PII at rest with authenticated encryption, resolve delayed linkages dynamically, enforce relational consistency, and generate five exactly formatted reports without data loss or metric distortion.

---

## 2. Technology Selection & Rationale

| Component | Choice | Justification & Rejected Alternatives |
|---|---|---|
| **Language & Runtime** | Python 3.11+ | Mandatory assignment requirement. Robust standard library, type annotations, and performance for data parsing. |
| **Database** | SQLite 3 (WAL mode) | Recommended and accepted in specification. Eliminates external operational dependencies (Docker, Postgres networking) while providing strict ACID transactions, foreign key enforcement, unique constraints, and high read/write throughput in Write-Ahead Logging (`PRAGMA journal_mode=WAL;`). |
| **PII Encryption** | AES-256-GCM (`cryptography` library) | Authenticated Encryption with Associated Data (AEAD). Provides both confidentiality and tamper detection. Guarantees that ciphertext cannot be swapped or forged. Avoided AES-CBC/ECB (vulnerable to padding oracle and lack authentication). |
| **Deterministic Phone Matching** | HMAC-SHA256 (`hashlib`, `hmac`) | Provides constant-time, collision-resistant deterministic tokenization for matching leads to events without decrypting phones or storing plaintext. Dedicated secret key prevents offline dictionary/rainbow table attacks. Avoided raw SHA-256 (trivially reversible for 10-digit Indian phones). |
| **CLI & Argument Parsing** | `argparse` (Standard Library) | Zero-dependency, standards-compliant command line interface supporting `init`, `ingest`, and `report`. Avoided heavy frameworks (Click, Typer) which add no necessary capability. |
| **Data Parsing & Decimal Arithmetic** | `csv`, `json`, `datetime`, `decimal` (Standard Library) | Prevents IEEE 754 floating-point inaccuracies in currency computations. Uses `decimal.Decimal` with explicit rounding. Dynamic schema parsing via stdlib `json`. |
| **Testing Framework** | `pytest` | Standard Python testing harness for parameterized unit tests, property tests, adversarial cases, and end-to-end integration workflows. |

---

## 3. End-to-End Architecture & Data Flow

```
+-----------------------------------------------------------------------------------+
|                                INPUT ARTIFACTS                                    |
|  - Drop N: leads/<workflow_id>/<batch>.csv                                        |
|  - Drop N: events/<events_xxxx>.jsonl                                             |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                        INGESTION & DISCOVERY LAYER (cli.py)                       |
|  1. Deterministic file sorting: drops -> batch files (alphabetical) -> events     |
|  2. File-level idempotency check: SHA-256 content hash in ingestion_files         |
+-----------------------------------------------------------------------------------+
             |                                                  |
             v (Leads)                                          v (Events)
+-------------------------------+      +--------------------------------------------+
|     LEAD PIPELINE (leads.py)   |      |         EVENT PIPELINE (events.py)         |
| 1. Header mapping & aliases   |      | 1. Parse JSONL; detect malformed lines     |
| 2. Row counting (skip blank)  |      | 2. Deduplicate event_id globally           |
| 3. Type normalization         |      | 3. Validate disposition codes vs catalogue |
| 4. Validation in schema order |      | 4. Normalize phone -> HMAC lookup token    |
|    - Invalid -> Rejections    |      | 5. Link to existing lead OR mark ORPHAN    |
|    - Optional bad -> Warning  |      | 6. Append to immutable interaction_events  |
| 5. PII Encryption (AES-GCM)   |      +--------------------------------------------+
| 6. Lead Merge (Upsert non-null|                             |
|    keeping initial batch_id)  |                             v
+-------------------------------+      +--------------------------------------------+
             |                         |     DELAYED RESOLUTION ENGINE (events.py)  |
             +------------------------>| On lead ingest, re-evaluate quarantine:    |
                                       | - Match pending orphans via phone HMAC     |
                                       | - Resolve overrides via call_id            |
                                       +--------------------------------------------+
                                                              |
                                                              v
+-----------------------------------------------------------------------------------+
|                         PERSISTENCE ENGINE (database.py)                          |
|  - Ingestion Files (receipts)                                                     |
|  - Leads (encrypted PII, HMAC token, external ref, metadata)                      |
|  - Lead Row Rejections & Field Warnings                                           |
|  - Immutable Interaction Events (append-only ledger)                              |
|  - Quality Metrics Ledger                                                         |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                         REPORTING ENGINE (reporting.py)                           |
|  1. Derived Business Facts: Calls (initiated), Connected, Effective Dispositions  |
|  2. Lead Bucket Precedence: yet_to_reach_out -> completed -> telephony_failed ->  |
|     in_progress_dialed                                                            |
|  3. Output Artifacts:                                                             |
|     - batch_summary.csv                                                           |
|     - daily_overview.csv                                                          |
|     - lead_report.csv                                                             |
|     - rejections.csv                                                              |
|     - data_quality.json                                                           |
+-----------------------------------------------------------------------------------+
```

---

## 4. Database Schema Design & Invariants

All relational constraints are enforced in SQLite using foreign keys (`PRAGMA foreign_keys = ON;`).

### A. Entity Relationship Diagram (Conceptual)

```
 [ingestion_files] (tracks file fingerprints & idempotency)
        |
        +---------------------------------------------+
        |                                             |
        v (1:N)                                       v (1:N)
     [leads] <----+                              [interaction_events] (immutable ledger)
        |         | (1:N)                             |
        |         +-----------------------------------+ (via lead_id, nullable when orphaned)
        v (1:N)
 [lead_rejections]
```

### B. Table Definitions & DDL

```sql
-- Track ingested files for idempotency
CREATE TABLE IF NOT EXISTS ingestion_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    drop_name TEXT NOT NULL,
    file_type TEXT NOT NULL CHECK (file_type IN ('leads', 'events')),
    workflow_id TEXT,
    batch_id TEXT,
    relative_path TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

-- Master lead table (one row per workflow_id + canonical phone)
CREATE TABLE IF NOT EXISTS leads (
    lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_uuid TEXT NOT NULL UNIQUE,      -- Immutable internal lead UUID for AEAD context
    workflow_id TEXT NOT NULL,
    phone_lookup_hmac TEXT NOT NULL,     -- HMAC-SHA256 of canonical phone (+91XXXXXXXXXX)
    encrypted_phone TEXT NOT NULL,       -- Base64(nonce || ciphertext || tag)
    encrypted_customer_name TEXT NOT NULL,
    external_ref TEXT NOT NULL,          -- policy_number or order_id
    initial_batch_id TEXT NOT NULL,      -- Permanent batch_id of first valid appearance
    policy_expiry TEXT,                  -- Normalized YYYY-MM-DD
    premium_amount TEXT,                 -- Normalized decimal string (2 decimal places)
    order_date TEXT,                     -- Normalized YYYY-MM-DD
    product TEXT,                        -- String
    preferred_language TEXT NOT NULL,    -- Normalized lowercase enum (defaults to 'en')
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CONSTRAINT uq_lead_identity UNIQUE (workflow_id, phone_lookup_hmac)
);

CREATE INDEX IF NOT EXISTS idx_leads_lookup ON leads(workflow_id, phone_lookup_hmac);
CREATE INDEX IF NOT EXISTS idx_leads_uuid ON leads(lead_uuid);
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(workflow_id, initial_batch_id);
CREATE INDEX IF NOT EXISTS idx_leads_external_ref ON leads(workflow_id, external_ref);

-- Rejected lead rows audit
CREATE TABLE IF NOT EXISTS lead_rejections (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    file_path TEXT NOT NULL,
    CONSTRAINT uq_rejection_row UNIQUE (workflow_id, batch_id, row_number)
);

CREATE INDEX IF NOT EXISTS idx_rejections_sort ON lead_rejections(workflow_id, batch_id, row_number);

-- Immutable Interaction Event Ledger
CREATE TABLE IF NOT EXISTS interaction_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,       -- Deduplication boundary across all drops
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_raw TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,       -- Normalized ISO UTC for precise chronological ordering
    occurred_at_ist TEXT NOT NULL,       -- Normalized ISO IST for reporting & display
    phone_lookup_hmac TEXT,              -- HMAC token (NULL for overrides without phone)
    lead_id INTEGER REFERENCES leads(lead_id) ON DELETE SET NULL,
    is_orphan INTEGER NOT NULL DEFAULT 0, -- 1 if unlinked, 0 if resolved
    is_unknown_disposition INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,          -- Preserved raw JSON payload
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_call ON interaction_events(call_id);
CREATE INDEX IF NOT EXISTS idx_events_lead ON interaction_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_lookup ON interaction_events(workflow_id, phone_lookup_hmac);
CREATE INDEX IF NOT EXISTS idx_events_orphan ON interaction_events(is_orphan);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON interaction_events(occurred_at_utc);

-- Global data quality counters table (reconciled across runs)
CREATE TABLE IF NOT EXISTS quality_metrics (
    metric_key TEXT PRIMARY KEY,
    metric_value INTEGER NOT NULL DEFAULT 0
);
```

---

## 5. Idempotency Strategy

### File-Level Idempotency
- Before opening a file, compute its SHA-256 checksum.
- Query `ingestion_files` where `relative_path = ?`.
- **Case 1 (Exact Match):** If the file was previously ingested with the exact same hash, log that it is skipped. No counters are incremented, and no records are re-processed.
- **Case 2 (Content Changed):** If the relative path exists but the hash differs, raise an `IngestionIntegrityError` to prevent silent corruption or double-counting.
- **Case 3 (New File):** Process the file within a strict database transaction (`BEGIN IMMEDIATE`). Record the file receipt in `ingestion_files` upon transaction commit.

### Record-Level Idempotency
- **Leads:** Enforced via `UNIQUE(workflow_id, phone_lookup_hmac)`. If a row matching this identity is encountered:
  - If identical file re-run: already handled by file-level check.
  - If duplicate in another batch/row: increment `duplicate_lead_rows_merged`. Apply non-null fields to the existing lead using `UPDATE ... WHERE lead_id = ?`, while preserving `initial_batch_id`.
- **Events:** Enforced via `UNIQUE(event_id)`.
  - When an incoming event line contains an `event_id` already present in `interaction_events`, it is classified as a duplicate: increment `duplicate_events_dropped`, skip insertion, and advance.
  - Transactions ensure atomicity: if an event file ingestion fails halfway through, the transaction rolls back, leaving no half-inserted events or corrupted metrics.

---

## 6. Deferred Event Linking & Orphan Resolution

### The Challenge
`drop_1` may contain call events for a phone number whose lead CSV is only uploaded in `drop_2`. Additionally, `disposition.override` events contain a `call_id` and supervisor disposition but no `phone`.

### Design
1. **Append-Only Retention:** Unlinked events are stored in `interaction_events` with `is_orphan = 1` and `lead_id = NULL`. Raw phone numbers are stripped; only the `phone_lookup_hmac` is retained.
2. **Exclusion from Metrics:** All operational report aggregations filter on `WHERE is_orphan = 0 AND is_unknown_disposition = 0`.
3. **Resolution on Ingestion:**
   - Whenever new leads are inserted or updated, an orphan resolution pass runs within the same transaction:
     ```sql
     -- Step A: Link orphan events matching known leads by phone lookup HMAC
     UPDATE interaction_events
     SET lead_id = (
         SELECT l.lead_id FROM leads l
         WHERE l.workflow_id = interaction_events.workflow_id
           AND l.phone_lookup_hmac = interaction_events.phone_lookup_hmac
     ),
     is_orphan = 0
     WHERE is_orphan = 1 AND phone_lookup_hmac IS NOT NULL
       AND EXISTS (
         SELECT 1 FROM leads l
         WHERE l.workflow_id = interaction_events.workflow_id
           AND l.phone_lookup_hmac = interaction_events.phone_lookup_hmac
     );

     -- Step B: Link orphan override events whose call_id is now associated with a resolved lead
     UPDATE interaction_events
     SET lead_id = (
         SELECT sibling.lead_id
         FROM interaction_events sibling
         WHERE sibling.call_id = interaction_events.call_id
           AND sibling.lead_id IS NOT NULL
         LIMIT 1
     ),
     is_orphan = 0
     WHERE is_orphan = 1 AND event_type = 'disposition.override'
       AND EXISTS (
         SELECT 1 FROM interaction_events sibling
         WHERE sibling.call_id = interaction_events.call_id
           AND sibling.lead_id IS NOT NULL
     );
     ```
4. **Final Quality Metrics:** `orphan_events` in `data_quality.json` queries `SELECT COUNT(*) FROM interaction_events WHERE is_orphan = 1;` at report generation time, accurately reflecting the current unresolved state.

---

## 7. PII Security & Cryptographic Architecture

### Threat Model & Invariants
- Database backups, SQLite files, logs, and exception dumps must never expose customer names or phone numbers in plaintext.
- Phone numbers must be joinable without decrypting all database rows.

### Cryptographic Primitives
1. **Encryption Algorithm:** AES-256-GCM.
   - Master Encryption Key: 256 bits (32 bytes), securely generated using `os.urandom(32)`.
   - Nonce: Fresh, cryptographically secure 96-bit (12 bytes) nonce generated per field encryption via `os.urandom(12)`. Nonce reuse is strictly prohibited.
   - Storage format: `base64(nonce [12 bytes] || ciphertext || tag [16 bytes])`.
   - Authenticated Associated Data (AAD): The ciphertext is cryptographically bound to the immutable internal lead UUID:
     `AAD = f"{workflow_id}:{field_name}:{lead_uuid}".encode("utf-8")`.
     This binds encrypted fields to their exact lead record and field name, preventing ciphertext transplant attacks while remaining completely immune to mutations in external reference or batch metadata, and enforcing strict domain isolation from the HMAC lookup token.
2. **Deterministic Lookup Token:** HMAC-SHA256.
   - Lookup Secret Key: 256 bits (32 bytes), completely separate from the encryption key.
   - Lookup Token: `hmac.new(lookup_key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()`.
   - Because canonical phone is normalized (`+91XXXXXXXXXX`), identical phone numbers yield identical HMACs under the same key without leaking the underlying digits.
3. **Key Management & Storage:**
   - Keys are initialized via `cli.py init` and persisted to `.local/keys.json` with restricted file permissions (0600 on POSIX).
   - `.local/` and all `*.db` / `*.sqlite*` files are excluded in `.gitignore`.
   - When decrypting for masked display in reports (`+91******` + last 4 digits), decryption occurs strictly in-memory during final serialization.

---

## 8. Reporting Architecture & Derived Business Facts

### Derived Facts Calculation

1. **Call Identification:**
   A call exists if and only if a linked (`is_orphan = 0`) event with `event_type = 'call.initiated'` exists.
   - Call date is the IST date (`YYYY-MM-DD`) derived from the `occurred_at` timestamp of `call.initiated`.
2. **Connected Call:**
   A call is connected if there is a linked `call.connected` event with matching `call_id`.
3. **Effective Disposition:**
   For each distinct call:
   - Query all valid disposition events (`is_unknown_disposition = 0` and linked to the lead).
   - If one or more `disposition.override` events exist, select the one with the latest `occurred_at_utc`. Deterministic tie-breaker: largest `event_id`.
   - Else if one or more `disposition.set` events exist, select the one with the latest `occurred_at_utc`. Deterministic tie-breaker: largest `event_id`.
   - Else effective disposition is `NULL`.
4. **Lead Buckets (Evaluated in strict priority order):**
   - **Bucket 1 (`yet_to_reach_out`):** Lead has 0 initiated calls.
   - **Bucket 2 (`completed`):** At least one initiated call on the lead has an effective disposition marked `is_terminal = true` in the disposition catalogue.
   - **Bucket 3 (`in_progress_telephony_failed`):** Lead has ≥ 1 initiated call, and *every* initiated call has a corresponding `call.telephony_failed` event.
   - **Bucket 4 (`in_progress_dialed`):** All other leads.
5. **Masked Phone:**
   Format: `+91******` followed by the last 4 digits of the canonical phone. E.g., `+919876543210` -> `+91******3210`.

### Report Specifications

1. **`batch_summary.csv`**
   - Header: `workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed`
   - Grouping: `workflow_id, initial_batch_id`.
   - Invariant: `total_leads = yet_to_reach_out + in_progress_telephony_failed + in_progress_dialed + completed`.
   - Sort: `workflow_id ASC, batch_id ASC`.

2. **`daily_overview.csv`**
   - Header: `workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads`
   - Grouping: `workflow_id, ist_date` for dates having ≥ 1 initiated call.
   - `connectivity_rate`: `connected_calls / total_calls`, formatted with exactly 4 decimal places (`{:.4f}`).
   - `success_leads`: Count of distinct `lead_id`s having at least one call on that IST date whose effective disposition has `is_success = true`.
   - Sort: `workflow_id ASC, date ASC`.

3. **`lead_report.csv`**
   - Header: `workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success`
   - One row per lead in `leads`.
   - `last_call_at`: ISO-8601 IST string (e.g., `2026-10-14T17:55:24+05:30`) of the latest `call.initiated`. Empty string if `total_calls == 0`.
   - `current_disposition`: The effective disposition code of the most recent call (by `call.initiated` time) that has an effective disposition. Empty string if none.
   - `ever_success`: `true` if any call on the lead has an effective disposition with `is_success = true`, else `false`.
   - Sort: `workflow_id ASC, external_ref ASC`.

4. **`rejections.csv`**
   - Header: `workflow_id,batch_id,row_number,reason`
   - One row per rejected lead row. Contains NO PII.
   - Sort: `workflow_id ASC, batch_id ASC, row_number ASC`.

5. **`data_quality.json`**
   - Exact JSON object containing integer counts:
     - `lead_rows_total`
     - `lead_rows_rejected`
     - `rejections_by_reason` (dict mapping reason string to integer count)
     - `duplicate_lead_rows_merged`
     - `field_warnings`
     - `events_total`
     - `malformed_events`
     - `duplicate_events_dropped`
     - `orphan_events`
     - `unknown_disposition_events`

---

## 9. Assumption Register

| ID | Item | Decision / Interpretation | Rationale | Alternatives Considered |
|---|---|---|---|---|
| **A-1** | Decimal Rounding | `ROUND_HALF_UP` to 2 decimal places. | Standard financial/accounting practice in Indian commercial systems. | `ROUND_HALF_EVEN` (banker's rounding). |
| **A-2** | Day-First Ambiguity | Treat all slash/dash dates with ambiguous components as day-first (e.g. `02/03/2026` = 2nd March). | Explicitly instructed in specification: "Dates are day-first." | Guessing month-first based on values. |
| **A-3** | Effective Disposition Tie-Breaking | If two overrides or two dispositions have the exact same `occurred_at`, tie-break by `event_id DESC`. | Guarantees deterministic, reproducible report generation. | Arbitrary database rowid order. |
| **A-4** | Current Disposition When Latest Call Has None | Look back to the most recent call (by initiated timestamp) that *does* have an effective disposition. | Follows requirement: "effective disposition of the most recent call (by initiated time) that has one". | Returning empty string if the absolute latest call lacks a disposition. |
| **A-5** | Incomplete Calls Without Call Ended | If a call has `call.initiated` and `call.connected` but telephony was interrupted before `call.ended`, it still counts as initiated and connected. | Connectivity is established at connection; duration is optional. | Treating as telephony failed. |
| **A-6** | Events without `call.initiated` | If a call event arrives without an initiated event, it cannot establish a valid call per spec rule: "A call is counted only if its call.initiated event is present". | Prevents ghost calls from headless status updates. | Counting calls based on call_id presence. |
| **A-7** | Key Storage Location | Store keys in `.local/keys.json` with git-exclusion, overridable by environment variable `KEY_STORE_PATH`. | Safe local developer workflow without committing secrets. | Hardcoding keys or requiring external KMS. |
