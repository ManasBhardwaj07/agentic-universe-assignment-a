# Final Requirement Audit & Traceability Matrix
## Agentic Universe — Software Development Internship (Assignment A: Interaction Ledger & Reporting Pipeline)

This document provides complete, line-by-line requirement traceability for the final engineering solution. Every business rule, data contract, cryptographic guarantee, and output schema from the official assignment specification is mapped to its exact implementation function, automated test, verification status, and physical execution evidence.

---

## 1. Requirement Traceability Matrix

### A. Schema Loading & Dynamic Catalogue Discovery

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **Dynamic JSON Schema Discovery** | Schemas are discovered dynamically from directory (`*.json`) without hardcoded workflow names or field lists. | `solution/schemas.py::SchemaRegistry._load` | `tests/test_leads.py::test_rejection_precedence_in_schema_order` | **PASS** | `SchemaRegistry` globs `assignment-a-data/schemas/*.json` and dynamically parses `motor_renewal.json` and `policy_feedback.json`. |
| **Workflow Field Definitions** | Extracts `name`, `type`, `required`, `pii`, `reference`, `aliases`, `values`, and `default`. | `solution/schemas.py::FieldDefinition` | `tests/test_leads.py::test_optional_field_warnings_on_accepted_rows` | **PASS** | Dataclass `FieldDefinition` populated with all schema attributes directly from JSON definitions. |
| **Reference Field Resolution** | Resolves workflow business reference field (`policy_number` for `motor_renewal`, `order_id` for `policy_feedback`). | `solution/schemas.py::WorkflowSchema.reference_field` | `tests/test_leads.py::test_encryption_integrity_on_reference_mutation` | **PASS** | Evaluated dynamically via `field.reference == True` property on `WorkflowSchema`. |
| **Dynamic Disposition Catalogue** | Reads `dispositions.csv` dynamically without hardcoded disposition codes or terminal/success flags. | `solution/schemas.py::SchemaRegistry._load` | `tests/test_derived_facts.py::test_bucket_precedence_and_effective_disposition` | **PASS** | Loads `code`, `label`, `is_terminal`, `is_success` for each workflow into `self.dispositions`. |

---

### B. CSV Ingestion & Lead Deduplication

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **Header Normalization & Aliases** | Case-insensitive, trims whitespace, collapses `_` or spaces; matches canonical names or declared aliases. | `solution/normalization.py::normalize_header`<br>`solution/leads.py::LeadIngestor._map_headers` | `tests/test_normalization.py::test_normalize_header` | **PASS** | Maps `phone_number` $\rightarrow$ `phone`, `cust_name` $\rightarrow$ `customer_name`, handling reordered/extra columns. |
| **Blank Row Skipping** | Rows where every cell is empty or whitespace are skipped and NOT counted towards `lead_rows_total` or row numbering. | `solution/leads.py::LeadIngestor.ingest_csv` | `tests/test_leads.py::test_blank_rows_and_row_numbering` | **PASS** | Tested on 2 data rows with 2 interspersed blank/whitespace rows; `lead_rows_total` strictly equals 2. |
| **Indian Phone Normalization** | Strips non-digits; handles 12-digit `91...`, 11-digit `0...`, validates 10 digits starting with 6–9; formats as `+91XXXXXXXXXX`. | `solution/normalization.py::normalize_phone` | `tests/test_normalization.py::test_normalize_phone_valid_formats`<br>`test_normalize_phone_invalid` | **PASS** | Tested against +91, 91, 0 prefixes, landlines, 9-digit numbers, international numbers, and invalid prefixes. |
| **Day-First Date Parsing** | Parses `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY` (case-insensitive); rejects invalid leap dates (e.g. 31/02). | `solution/normalization.py::parse_date` | `tests/test_normalization.py::test_parse_date` | **PASS** | Day-first rule strictly enforced; impossible dates return `(None, False)`. Output normalized to `YYYY-MM-DD`. |
| **Decimal & Currency Normalization** | Strips `₹`, `Rs`, `Rs.`, `INR`, removes commas, rounds to 2 decimals using `ROUND_HALF_UP`, rejects negatives. | `solution/normalization.py::parse_decimal` | `tests/test_normalization.py::test_parse_decimal` | **PASS** | Tested with ₹, INR, commas, half-way rounding (`100.555` $\rightarrow$ `100.56`), and negative inputs. |
| **Schema-Order Rejection Precedence** | Rejections evaluated in exact schema field order; first invalid required field wins. | `solution/leads.py::LeadIngestor._process_row` | `tests/test_leads.py::test_rejection_precedence_in_schema_order` | **PASS** | Row with missing name and invalid phone rejected as `missing_required:customer_name` because name precedes phone in schema. |
| **Optional Field Warnings** | Invalid optional fields set to NULL, row is accepted, `field_warnings` incremented, row NOT rejected. | `solution/leads.py::LeadIngestor._process_row` | `tests/test_leads.py::test_optional_field_warnings_on_accepted_rows` | **PASS** | Invalid expiry and non-decimal premium stored as NULL, 2 field warnings recorded, lead stored in DB. |
| **Duplicate Lead Merging (Non-Null Last-Write-Wins)** | Same `(workflow_id, canonical_phone)` updates non-null fields; null fields do NOT overwrite existing data. | `solution/leads.py::LeadIngestor._upsert_lead` | `tests/test_leads.py::test_duplicate_lead_merging` | **PASS** | Duplicate row updates premium but preserves existing non-null preferred language; increments `duplicate_lead_rows_merged`. |
| **Initial Batch ID Permanence** | Duplicate lead rows keep the initial batch ID permanently; later batch IDs do not overwrite it. | `solution/leads.py::LeadIngestor._upsert_lead` | `tests/test_leads.py::test_duplicate_lead_merging` | **PASS** | Ingesting duplicate in `batch_002` keeps `initial_batch_id == 'batch_001'` permanently. |
| **Strict Validation for Duplicate Rows** | Duplicate CSV rows must pass required-field validation before merging. Rows missing required fields are rejected. | `solution/leads.py::LeadIngestor._process_row` | `tests/test_leads.py::test_duplicate_row_missing_required_field_rejected` | **PASS** | Duplicate missing `customer_name` rejected with `missing_required:customer_name`; does not update DB or increment merged count. |

---

### C. Event Ingestion & Append-Only Ledger

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **Empty Line Handling** | Whitespace/empty lines in JSONL are skipped and NOT counted towards `events_total`. | `solution/events.py::EventIngestor.ingest_jsonl` | `tests/test_events.py::test_malformed_and_duplicate_events` | **PASS** | Tested with empty and whitespace-only lines; `events_total` increments only on non-empty lines. |
| **Malformed Event Check Precedence** | Malformed JSON or non-dict/missing `event_id` counted as `malformed_events` before duplicate check. | `solution/events.py::EventIngestor.ingest_jsonl` | `tests/test_events.py::test_malformed_and_duplicate_events` | **PASS** | Malformed JSON syntax and missing string `event_id` increment `malformed_events`. |
| **Global Duplicate Event Drop** | Duplicate `event_id` dropped globally across batches/drops; increments `duplicate_events_dropped`. | `solution/events.py::EventIngestor.ingest_jsonl` | `tests/test_events.py::test_duplicate_events_across_drops` | **PASS** | Re-delivering event `e01` in Drop 2 drops the duplicate and increments `duplicate_events_dropped`. |
| **Unknown Disposition Validation** | `disposition.*` events with invalid code for workflow increment `unknown_disposition_events` and flag `is_unknown_disposition = 1`. | `solution/events.py::EventIngestor._insert_event` | `tests/test_events.py::test_unknown_disposition_handling_and_orphan_resolution` | **PASS** | Invalid code flagged in database; excluded from effective disposition computation. |
| **Phone Linkage & Tokenization** | Canonical phone mapped to HMAC-SHA256 token; raw phone number NEVER stored in event records. | `solution/events.py::EventIngestor._insert_event` | `tests/test_events.py::test_persisted_event_privacy` | **PASS** | Raw phone sanitized from payload (`[REDACTED]`); SQLite contains only `phone_lookup_hmac`. |
| **Call-ID Override Linkage** | `disposition.override` lacking phone links to lead through sibling events sharing `call_id`. | `solution/events.py::EventIngestor._insert_event` | `tests/test_derived_facts.py::test_bucket_precedence_and_effective_disposition` | **PASS** | Sibling lookup finds `lead_id` from existing call events. |
| **Delayed Orphan Resolution** | Events arriving before lead CSV quarantined as `is_orphan = 1`; resolved automatically when lead arrives. | `solution/events.py::EventIngestor.resolve_orphans` | `tests/test_events.py::test_delayed_orphan_resolution` | **PASS** | Events ingested before leads resolved upon subsequent lead batch ingestion; `is_orphan` updated to 0. |

---

### D. Derived Business Facts & Bucketing

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **Initiated Call Prerequisite** | A call is counted ONLY if its `call.initiated` event is present and linked to an accepted lead. | `solution/reporting.py::ReportGenerator._compute_call_facts` | `tests/test_derived_facts.py::test_bucket_precedence_and_effective_disposition` | **PASS** | Events with `call_id` lacking `call.initiated` do not create calls in reports. |
| **Connected Call Identification** | Call is connected if sibling `call.connected` event exists. | `solution/reporting.py::ReportGenerator._compute_call_facts` | `tests/test_derived_facts.py::test_bucket_precedence_and_effective_disposition` | **PASS** | Checked via sibling event matching on `call_id`. |
| **Effective Disposition & Overrides** | `disposition.override` overrides `disposition.set`; latest override by `occurred_at_utc` wins. | `solution/reporting.py::ReportGenerator._compute_call_facts` | `tests/test_derived_facts.py::test_bucket_precedence_and_effective_disposition` | **PASS** | Verified override changes effective disposition and terminal/success flags. |
| **Timestamp Tie-Breaking** | If multiple disposition events share identical timestamps, tie-break deterministically using `event_id DESC`. | `solution/reporting.py::ReportGenerator._compute_call_facts` | `tests/test_events.py::test_equal_timestamp_tie_breaking` | **PASS** | Deterministic sorting on `(occurred_at_utc, event_id)` tested with identical timestamps. |
| **Lead Bucket Precedence** | 1. `yet_to_reach_out` (0 calls)<br>2. `completed` (any terminal call)<br>3. `in_progress_telephony_failed` (every call failed)<br>4. `in_progress_dialed` (all other). | `solution/reporting.py::ReportGenerator._determine_bucket` | `tests/test_derived_facts.py::test_bucket_precedence_and_effective_disposition` | **PASS** | Strict priority order verified across all 4 bucket conditions. |

---

### E. Output Reports & Formatting Contracts

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **`batch_summary.csv`** | Headers: `workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed`. Sorted: `workflow_id ASC, batch_id ASC`. | `solution/reporting.py::ReportGenerator._generate_batch_summary` | `tests/test_independent_reconciliation.py::test_independent_data_reconciliation` | **PASS** | Reconciles bucket counts to `total_leads` for every batch. |
| **`daily_overview.csv`** | Headers: `workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads`. Sorted: `workflow_id ASC, date ASC`. Rate formatted `0.0000`. | `solution/reporting.py::ReportGenerator._generate_daily_overview` | `tests/test_independent_reconciliation.py::test_independent_data_reconciliation` | **PASS** | Daily calls, connected calls, 4-decimal connectivity rate, and distinct daily success leads match independent calculation. |
| **`lead_report.csv`** | Headers: `workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success`. Masked: `+91******XXXX`. Sorted: `workflow_id ASC, external_ref ASC`. | `solution/reporting.py::ReportGenerator._generate_lead_report` | `tests/test_independent_reconciliation.py::test_independent_data_reconciliation`<br>`tests/test_normalization.py::test_mask_phone` | **PASS** | Phone masked as `+91******XXXX`, `ever_success` is boolean string (`true`/`false`), sorted by reference. |
| **`rejections.csv`** | Headers: `workflow_id,batch_id,row_number,reason`. Sorted: `workflow_id ASC, batch_id ASC, row_number ASC`. Contains zero PII. | `solution/reporting.py::ReportGenerator._generate_rejections` | `tests/test_leads.py::test_rejection_precedence_in_schema_order`<br>`tests/test_security.py::test_no_plaintext_pii_in_database` | **PASS** | Verified row-level audit trail without names, phones, or reference payloads. |
| **`data_quality.json`** | Exact 10-key JSON structure matching client specification. | `solution/reporting.py::ReportGenerator._generate_data_quality` | `tests/test_independent_reconciliation.py::test_independent_data_reconciliation` | **PASS** | Exact keys: `lead_rows_total`, `lead_rows_rejected`, `rejections_by_reason`, `duplicate_lead_rows_merged`, `field_warnings`, `events_total`, `malformed_events`, `duplicate_events_dropped`, `orphan_events`, `unknown_disposition_events`. |

---

### F. Idempotency & Fault Tolerance

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **Logical Source File Identity** | File identity based on logical source: `{drop_name}/leads/{wf}/{file}` and `{drop_name}/events/{file}`. | `solution/leads.py`<br>`solution/events.py`<br>`solution/database.py` | `tests/test_integration.py::test_distinct_batches_identical_content_processed_and_merged` | **PASS** | Distinct batch files (`batch_001.csv` and `batch_002.csv`) with identical contents are both ingested, merging duplicate rows. |
| **Replay Idempotency** | Re-ingesting the exact same logical file path with matching content hash skips processing without modifying metrics. | `solution/database.py::check_file_status` | `tests/test_integration.py::test_sequential_and_reingest_idempotency` | **PASS** | Re-running Drop 1 and Drop 2 produces zero metric change and byte-for-byte identical reports. |
| **Content Tampering Detection** | Re-ingesting a previously ingested logical path with altered content raises an error to prevent silent corruption. | `solution/database.py::check_file_status` | `tests/test_integration.py::test_idempotency_same_path_changed_content` | **PASS** | Raises `ValueError: File ... has changed since previous ingestion. Cannot safely re-ingest.` |
| **Crash Atomicity** | Ingestion occurs within SQLite transactions (`with conn:`). Interrupted ingestion rolls back all changes cleanly. | `solution/cli.py::_discover_and_ingest_drop` | `tests/test_integration.py::test_interrupted_ingestion_and_retry` | **PASS** | Simulated mid-file crash rolls back all partial inserts, allowing safe retry from pristine state. |

---

### G. Security & Cryptographic Invariants

| Requirement | Exact expected behaviour | Implementation file/function | Test file/function | Status | Evidence |
|---|---|---|---|:---:|---|
| **AES-256-GCM Authenticated Encryption** | 256-bit AES key; fresh 96-bit random nonce (`os.urandom(12)`) per encryption; 128-bit authentication tag. | `solution/crypto.py::CryptoManager.encrypt_field` | `tests/test_security.py::test_aes_gcm_fresh_nonce_per_encryption` | **PASS** | Encrypting identical string twice produces distinct ciphertexts; both decrypt successfully. |
| **Immutable Lead UUID AAD** | AEAD context bound to immutable internal lead UUID (`f"{workflow_id}:{field_name}:{lead_uuid}"`). | `solution/leads.py::LeadIngestor._upsert_lead` | `tests/test_leads.py::test_encryption_integrity_on_reference_mutation`<br>`test_lead_uuid_aad_reference_mutation_independent_of_name` | **PASS** | Reference mutations in duplicate rows never break decryption; tampering with AAD raises `InvalidTag`. |
| **Key Isolation (AES vs HMAC)** | Separate 256-bit secret keys for AES-GCM and HMAC-SHA256. | `solution/crypto.py::CryptoManager` | `tests/test_security.py::test_hmac_deterministic_lookup_and_key_isolation` | **PASS** | Key separation prevents domain-confusion attacks; HMAC key cannot decrypt AES tokens. |
| **Fail-Closed Key Management** | Missing key file on an existing encrypted database halts execution rather than generating new keys. | `solution/cli.py::cmd_ingest`<br>`solution/crypto.py::CryptoManager.load` | `tests/test_security.py::test_missing_key_file_fail_closed` | **PASS** | Raises `FileNotFoundError` / halts with exit code 1 to protect existing data from unrecoverable overwrites. |
| **Zero Plaintext PII at Rest** | No raw customer names or phone numbers stored in SQLite tables, event payloads, or rejection logs. | `solution/leads.py`<br>`solution/events.py` | `tests/test_security.py::test_no_plaintext_pii_in_database` | **PASS** | Automated scanner searches raw SQLite binary bytes on disk; verifies zero plaintext occurrences. |
