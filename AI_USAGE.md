# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- Antigravity AI Assistant (Google Gemini 3.8 Flash)
- Python standard library & pytest documentation

## What I used them for
- Rapid exploration and structural parsing of the assignment specification, schemas, and data generator rules.
- Scaffolding the initial SQLite schema definitions and table structures.
- Generating parameterized regex patterns for Indian phone and multi-format date normalization.
- Drafting initial boilerplate for CLI subcommands (`init`, `ingest`, `report`) with `argparse`.
- Generating comprehensive test case matrices in `tests/test_normalization.py` for edge cases in phone prefixes, date representations, and currency symbols.

## What I wrote or decided myself
- **Cryptographic Threat Model & Invariant Architecture:** Decided on AES-256-GCM authenticated encryption paired with separate HMAC-SHA256 deterministic tokenization. Explicitly rejected using plain SHA-256 for phone hashing to prevent rainbow table precomputation. Designed the Authenticated Associated Data (AAD) structure to bind ciphertexts to their workflow and record context.
- **Delayed Orphan Resolution Strategy:** Formulated the two-phase relational query approach (phone token join for regular events, call-id sibling join for `disposition.override`) to resolve unlinked events as later drops are ingested without mutating event history.
- **Relational Integrity & Ledger Immutability:** Designed database constraints (`UNIQUE(event_id)`, `UNIQUE(workflow_id, phone_lookup_hmac)`) and Write-Ahead Logging (`WAL`) transactional boundaries so that file receipts commit atomically with data rows.
- **Lead Duplicate Merging Logic:** Defined the last-write-wins non-null update rule while enforcing the permanence of the `initial_batch_id` and applying default language only when null.
- **Report Contract Reconciliation:** Formulated the tie-breaking rules and chronological fallback logic for effective disposition and lead bucket precedence.

## Where the AI was wrong or unhelpful
- **AI Suggestion on AES-GCM Associated Data (AAD):** The AI originally proposed constructing AAD as `workflow_id:field_name:external_ref`. During adversarial review, I realized that `external_ref` (`policy_number` / `order_id`) can change if a duplicate lead row arrives in a later batch with an updated reference. This would invalidate the AAD authentication tag on existing encrypted fields and cause `InvalidTag` exceptions during decryption.
  - *How I fixed it:* I bound AAD to an immutable internal lead UUID (`lead_uuid`): `f"{workflow_id}:{field_name}:{lead_uuid}"`, guaranteeing cryptographic integrity across arbitrary reference mutations while maintaining strict domain separation between HMAC lookup tokens (`phone_lookup_hmac`) and AES authenticated encryption.
- **AI Suggestion on File-Level Idempotency:** The AI initially proposed global content-hash deduplication for all incoming files. During spec audit, I caught that two distinct CSV batches (e.g. `batch_001.csv` and `batch_002.csv`) containing identical data must not be deduplicated away globally—they represent separate logical batches whose matching rows must be counted as duplicate lead rows.
  - *How I fixed it:* I anchored file identity to a stable logical source identity (`{drop_name}/leads/{workflow_id}/{filename}` and `{drop_name}/events/{filename}`). This ensures distinct batches with identical bytes are processed and merged properly, while replaying the same logical drop remains completely idempotent.
- **AI Suggestion on Duplicate Row Validation:** An earlier refactoring proposed allowing duplicate rows to skip required-field validation so that they could update non-null fields without needing all required fields present.
  - *Why this was rejected:* The assignment specification requires every accepted CSV row to satisfy required-field validation. Allowing duplicate rows missing required fields (such as `customer_name`) would violate the schema contract.
  - *How I fixed it:* I enforced schema-order validation across all rows before upserting. If a duplicate row lacks a required field, it is rejected with `missing_required:<field>`, recorded in `lead_rejections`, and neither updates the lead nor increments `duplicate_lead_rows_merged`. Verified by `test_duplicate_row_missing_required_field_rejected`.
- **AI Stale Dictionary Reference in Reporting:** When migrating AAD binding from `phone_lookup_hmac` to `lead_uuid`, the SQL query in `_generate_lead_report` was updated to `SELECT lead_id, ..., lead_uuid`, but the subsequent code retained an obsolete `row["phone_lookup_hmac"]` dictionary access.
  - *How I fixed it:* Caught via automated test execution (`IndexError`). Cleaned up the query and directly bound phone decryption to `row["lead_uuid"]`.
- **AI Over-Broad Ignore Pattern in Verification Tooling:** The AI used a glob pattern `data*` in the isolated release test script to ignore the `data/` directory, which inadvertently matched and excluded `database.py`.
  - *How I fixed it:* Replaced glob patterns with an explicit set of directory paths (`data/`, `reports/`, `.local/`, `.venv/`), ensuring critical Python modules starting with `data` are never ignored.
- **AI Suggestion on Key Loading (Fail-Closed Enforcement):** The AI initially used `CryptoManager.load_or_create()` in the `ingest` subcommand, which would automatically generate a fresh key pair if `.local/keys.json` was missing.
  - *Why this was dangerous:* If an existing database already contained encrypted leads, generating new keys would render all previously encrypted PII permanently unreadable.
  - *How I fixed it:* Enforced fail-closed key loading: if a database file exists but the key file is missing, `cli.py` halts immediately with an error and refuses to generate a new key pair. Verified by `test_missing_key_file_fail_closed`.
- **AI Suggestion on Duplicate Leads:** The AI initially proposed implementing duplicate lead merging by writing each duplicate as a separate record in `leads` with a version column and computing the latest state at query time.
  - *Why this was rejected:* The assignment specification requires a unique lead identity `(workflow_id, canonical phone)` with `batch_id` permanently bound to the first valid file stem, and reports require one row per lead. Performing repeated joins across historical versions would complicate `batch_summary.csv` and compromise relational uniqueness constraints. I rejected this in favor of an explicit upsert pattern that preserves `initial_batch_id` and updates only non-null fields on duplicate arrival.
- **AI Suggestion on Phone Normalization:** The AI drafted a phone normalization regex that stripped country codes using `.replace("+91", "")` without checking if the remaining digits formed a 10-digit number starting with 6–9. This would have accepted invalid landlines and corrupted numbers with embedded "91".
  - *How I fixed it:* I replaced it with a strict multi-step pipeline: extracting raw digits, checking exact length boundaries (12 digits with 91 prefix, 11 digits with 0 prefix, exactly 10 digits starting with 6, 7, 8, or 9), and rejecting any invalid formats.

## How I verified AI-generated code
1. **Automated Test Suite:** Created a comprehensive test suite of 33 automated unit, security, adversarial, and integration tests with `pytest`.
2. **Binary Database Inspection:** Wrote an automated security test (`test_no_plaintext_pii_in_database`) that directly scans raw SQLite binary database bytes on disk to prove that no plaintext customer names or phone numbers ever touch the database file.
3. **Idempotency & Diff Verification:** Executed sequential ingestion (`drop_1` followed by `drop_2`), generated reports, and re-ingested both drops. Compared all five generated report files before and after re-ingestion with automated file diffing to prove byte-identical output across multiple seeds.
4. **Independent Data Reconciliation:** Wrote `test_independent_data_reconciliation` which reads raw CSV and JSONL files directly, computes totals independently, and reconciles all 5 report outputs.
5. **Large Scale Benchmarking:** Benchmarked the pipeline against ~140,000 leads and ~613,000 events (`--scale large`), measuring actual memory, runtime, and database size.
6. **Manual Code Review:** Reviewed every function line by line to verify correct error handling, decimal precision handling with `ROUND_HALF_UP`, and transactional consistency.
