# Final Repository Inventory & Documentation Decisions
## Agentic Universe — SDE Internship Assignment A

This document details the final file inventory, classifications, documentation audit decisions, and Git state for the submission repository.

---

## 1. Documentation Audit & Decision Table

Per Phase Three review, internal development scratchpads and speculative enterprise architecture proposals were reviewed to ensure a clean, focused, non-duplicative documentation set:

| Document | Original or Created | Required by Assignment? | Useful to Evaluator? | Duplicates Other Content? | Final Decision | Rationale |
|---|---|---|---|---|:---:|---|
| **`docs/GLOSSARY.md`** | Original | Yes (Supplied) | High | No | **RETAIN UNCHANGED** | Official assignment domain terminology and definitions. Pristine upstream file. |
| **`docs/solution-design.md`** | Created | Recommended | High | No | **RETAIN** | Comprehensive technical specification: SQLite WAL schema, AES-256-GCM AEAD encryption, HMAC lookup tokenization, delayed orphan resolution, and failure recovery. |
| **`docs/final-requirement-audit.md`** | Created | Recommended | High | No | **RETAIN** | Full requirement-to-implementation and requirement-to-test traceability matrix. Maps every business rule to automated tests and execution evidence. |
| **`docs/pre-submission-audit.md`** | Created | No (Internal) | Low | Yes | **REMOVED** | Internal development audit scratchpad. All verified findings (AAD mutation resilience, pre-merge validation, logical file identity) are captured in `AI_USAGE.md` and `docs/final-requirement-audit.md`. |
| **`docs/production-readiness.md`** | Created | No (Internal) | Low | Yes | **REMOVED** | Contained speculative enterprise architecture proposals (Kafka, PostgreSQL, Prometheus) outside the scope of this assignment. Grounded failure mode mitigations are already documented in `docs/solution-design.md` and `SUBMISSION.md`. |

---

## 2. Complete Repository Inventory & Classification

Every file in the submission repository is classified under one of the defined roles:

| Path | Classification | Purpose / Rationale |
|---|---|---|
| **`README.md`** | Required documentation | Official Agentic Universe assignment prompt and instructions. Preserved 100% unmodified. |
| **`SUBMISSION.md`** | Required documentation | Primary evaluator document: problem selection, architecture summary, trade-offs, test results, and clean-clone run instructions. |
| **`AI_USAGE.md`** | Required documentation | Transparent engineering disclosure: tools used, human decision ownership, six real technical incidents caught/fixed, and verification methodology. |
| **`requirements.txt`** | Required implementation | Dependency manifest: preserves upstream mock requirements (`fastapi`, `uvicorn`, `httpx`), test runner (`pytest`), and AEAD cryptography (`cryptography`). |
| **`.gitignore`** | Supporting tooling | Explicit exclusions for Python bytecode, virtualenvs (`.venv/`, `.gate_venv/`), local databases (`.local/`), datasets (`data/`), and reports (`reports/`). |
| **`docker-compose.yml`** | Supporting tooling | Original supplied multi-service container orchestration file. Preserved unmodified. |
| **`assignment-a-data/README.md`** | Required documentation | Original Problem A specification, schemas, and output contracts. Preserved unmodified. |
| **`assignment-a-data/schemas/dispositions.csv`** | Required implementation | Official disposition code catalogue (terminal and success flags). Preserved unmodified. |
| **`assignment-a-data/schemas/motor_renewal.json`** | Required implementation | Official workflow JSON schema for Motor Renewal leads. Preserved unmodified. |
| **`assignment-a-data/schemas/policy_feedback.json`** | Required implementation | Official workflow JSON schema for Policy Feedback leads. Preserved unmodified. |
| **`assignment-a-data/tools/generate_data.py`** | Supporting tooling | Official synthetic test data generator for small and large scales. Preserved unmodified. |
| **`assignment-b-workflow/*`** | Supporting tooling | Original Assignment B materials and mock telephony provider. Preserved unmodified per assignment instructions. |
| **`docs/GLOSSARY.md`** | Required documentation | Official domain glossary. Preserved unmodified. |
| **`docs/solution-design.md`** | Required documentation | Core architecture, database relational model, cryptographic design, and derived fact state machine. |
| **`docs/final-requirement-audit.md`** | Required documentation | Line-by-line requirement traceability matrix covering all specification rules. |
| **`docs/repository-inventory.md`** | Required documentation | This document: inventory, classifications, documentation audit decisions, and Git state. |
| **`solution/__init__.py`** | Required implementation | Package initialization. |
| **`solution/cli.py`** | Required implementation | Command-line interface (`init`, `ingest`, `report`) built with standard library `argparse`. |
| **`solution/crypto.py`** | Required implementation | AES-256-GCM AEAD field encryption (fresh 96-bit nonces, immutable UUID AAD) and HMAC-SHA256 phone tokens. |
| **`solution/database.py`** | Required implementation | SQLite schema DDL, connection factory (`WAL` mode, foreign keys), file-receipt idempotency, and atomic transactions. |
| **`solution/schemas.py`** | Required implementation | Dynamic JSON workflow schema loader and CSV disposition catalogue parser. |
| **`solution/normalization.py`** | Required implementation | Normalizers: headers, Indian mobile phones, day-first dates, decimal currencies (`ROUND_HALF_UP`), enums, and timestamps (UTC/IST). |
| **`solution/leads.py`** | Required implementation | CSV lead ingestion, blank row skipping, schema-order rejections, optional field warnings, and duplicate merging. |
| **`solution/events.py`** | Required implementation | JSONL interaction ledger, malformed checks, global deduplication, phone redaction, and delayed orphan resolution. |
| **`solution/reporting.py`** | Required implementation | Derived facts calculation, bucket evaluation, and generation of all five report artifacts. |
| **`solution/benchmark.py`** | Supporting tooling | Performance benchmarking tool measuring runtime, peak memory, and record counts on small and large scales. |
| **`solution/README.md`** | Required documentation | Implementation-level technical documentation and CLI execution guide. |
| **`tests/__init__.py`** | Supporting tests | Test package initialization. |
| **`tests/test_normalization.py`** | Supporting tests | Normalization unit tests (phone formats, dates, decimals, enums, headers, masking). |
| **`tests/test_leads.py`** | Supporting tests | Lead validation tests, blank rows, rejection precedence, optional warnings, duplicate merging, and AAD mutation resilience. |
| **`tests/test_events.py`** | Supporting tests | Event ledger tests, malformed JSON, duplicate dropping, unknown dispositions, delayed orphans, and timestamp tie-breaking. |
| **`tests/test_derived_facts.py`** | Supporting tests | Call counting, telephony failure handling, disposition override precedence, and lead bucket priority. |
| **`tests/test_security.py`** | Supporting tests | Fresh nonces, AAD tamper detection, key isolation, fail-closed key loading, and binary SQLite PII inspection. |
| **`tests/test_integration.py`** | Supporting tests | Sequential vs parent ingestion equivalence, distinct batches with identical bytes, changed content detection, and crash atomicity. |
| **`tests/test_independent_reconciliation.py`** | Supporting tests | Independent test oracle reading raw files to mathematically reconcile all 5 report outputs. |

---

## 3. Temporary Artifacts Cleaned Up

The following temporary scripts and intermediate working files were created during testing and have been removed:
- `run_release_gate.py`: Temporary clean-checkout test runner. Removed.
- `gate_evidence.json`: Temporary verification log. Removed.
- `apply_patch.py`: Temporary PowerShell escaping workaround. Removed.
- `verify_fresh_seed.py`: Temporary seed verification runner. Removed.
- `docs/pre-submission-audit.md`: Working audit draft. Removed (incorporated into `final-requirement-audit.md` and `AI_USAGE.md`).
- `docs/production-readiness.md`: Speculative draft. Removed (grounded parts in `solution-design.md`).

---

## 4. Git State & Outstanding Dependencies

- **Branch:** `main`
- **Upstream Remote:** `https://github.com/AryamannNingombam/sde-assignment-junior-2026.git` (Pristine original repository; never receives implementation pushes).
- **Target Origin Remote:** `https://github.com/ManasBhardwaj07/agentic-universe-assignment-a.git` (Candidate's new private repository).
- **Outstanding Dependencies:**
  - Remote repository creation and initial push to `origin main`.
  - Independent clone verification from GitHub into a fresh directory.
  - Reviewer invitation (pending provision of reviewer GitHub handles by hiring team).
