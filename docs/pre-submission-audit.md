# Pre-Submission Engineering Audit Record

## 1. Environment & Repository Reconnaissance
- **Repository Root:** `C:/sde-assignment-junior-2026`
- **Active Branch:** `main`
- **Remote Origin:** `https://github.com/AryamannNingombam/sde-assignment-junior-2026.git`
- **Python Version:** 3.11.9
- **Virtual Environment:** `C:/sde-assignment-junior-2026/.venv`
- **Declared Dependencies (`requirements.txt`):**
  - `fastapi>=0.110` (Assignment B mock requirement)
  - `uvicorn>=0.29` (Assignment B mock requirement)
  - `httpx>=0.27` (Assignment B mock requirement)
  - `pytest>=8.0` (Test runner)
  - `cryptography>=42.0.0` (AES-256-GCM AEAD encryption)
- **Git Status:**
  - Modified: `.gitignore`, `AI_USAGE.md`, `SUBMISSION.md`, `requirements.txt`, `solution/README.md`
  - Untracked: `docs/solution-design.md`, `solution/*.py`, `tests/*.py`
  - Root `README.md` is confirmed 100% clean and unmodified (`git diff -- README.md` is empty).

---

## 2. Adversarial Findings & Critical Corrections

### A. Authenticated Associated Data (AAD) Mutation Vulnerability
- **Initial Implementation:** AAD was constructed as `f"{workflow_id}:{field_name}:{external_ref}"`.
- **Finding:** In duplicate lead merging, a subsequent CSV row can deliver an updated `external_ref` (`policy_number` / `order_id`) without supplying a new `customer_name`. Decrypting the previously stored `encrypted_customer_name` using the mutated `external_ref` caused authentication failure (`InvalidTag`), breaking report generation.
- **Resolution:** Bound AAD to an immutable internal lead UUID (`lead_uuid`):
  `AAD = f"{workflow_id}:{field_name}:{lead_uuid}".encode("utf-8")`.
  Because `lead_uuid` is an immutable UUID4 assigned once upon lead creation, reference mutations across batches never invalidate ciphertexts, while preserving strict domain separation from the HMAC phone lookup key.

### B. Duplicate Lead Merging & Pre-Merge Validation
- **Finding:** Every non-blank row in incoming CSV batches must pass required-field validation before duplicate merging. If a duplicate row arrives with a missing required field (e.g. missing `customer_name`), it must be rejected rather than merged.
- **Resolution:** Enforced schema-order validation across all rows. Invalid duplicates are rejected with `missing_required:<field>`, recorded in `lead_rejections`, and neither modify database records nor increment `duplicate_lead_rows_merged`. Valid duplicate rows update non-null fields using last-write-wins.

### C. Logical File Identity & Replay Idempotency
- **Finding:** Deduplicating files purely by global content-hash caused two distinct legitimate batch files (`batch_001.csv` and `batch_002.csv`) containing identical data to drop the second batch.
- **Resolution:** Anchored file receipts to stable logical source identities (`{drop_name}/leads/{workflow_id}/{file.name}` and `{drop_name}/events/{file.name}`):
  - Same logical path, same hash: Skip (idempotent replay).
  - Same logical path, changed hash: Raise `ValueError` preventing silent corruption.
  - Distinct batch files with identical data: Ingested and duplicate rows properly merged.
  - New file: Ingest inside an atomic transaction.

### D. Event Payload Phone Number Sanitization
- **Finding:** While top-level `phone` was tokenized into `phone_lookup_hmac` and omitted from the database columns, adversarial inputs could attempt to leak phone numbers inside `payload`.
- **Resolution:** Added active key sanitization in `solution/events.py` so any `"phone"`-related keys inside `payload` are redacted before JSON serialization into `payload_json`.

---

## 3. Verified Invariants
- **Append-Only Event Ledger:** Overrides create independent events linked via `call_id`. Original event records are never edited or rewritten.
- **Delayed Orphan Resolution:** Quarantined events (`is_orphan = 1`) link dynamically when matching leads arrive in later drops. Final orphan count in `data_quality.json` reflects only unresolved final-state events.
- **Database Privacy Audit:** Binary scanning of `.local/*.db` confirmed that no customer names, raw phone numbers, or unhashed phones exist in SQLite storage.
- **Report Determinism:** Diffing reports generated from sequential drops vs re-ingestion confirmed 100% byte-for-byte identical output across all five report artifacts.
