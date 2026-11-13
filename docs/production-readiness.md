# Production Readiness & Operations Architecture

This document provides an engineering evaluation of the Interaction Ledger & Reporting Pipeline for production deployment at Agentic Universe. It details failure modes, cryptographic lifecycle management, operational telemetry, and the roadmap for scaling from single-node SQLite to a distributed enterprise data architecture.

---

## 1. Database Reliability, WAL Mode & Disaster Recovery

### Write-Ahead Logging (WAL) Architecture
The production database operates in SQLite `WAL` mode (`PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL;`):
- **Concurrency:** Readers do not block writers, and writers do not block readers. Morning operational report queries can run concurrently without locking incoming event ingestion transactions.
- **Single-Writer Constraint:** SQLite serializes write transactions at the database file level. While adequate for sequential batch drops (processing 600k events in ~10 minutes), concurrent horizontal worker pods writing to the same SQLite file will encounter `sqlite3.OperationalError: database is locked`.

### Backup & Point-In-Time Recovery
- **Hot Backups:** Never copy raw `.db` files while WAL transactions are active. In production, utilize SQLite Online Backup API via Python (`conn.backup(backup_conn)`) or `sqlite3 data.db ".backup backup.db"`.
- **Integrity Audits:** Periodic automated execution of `PRAGMA integrity_check;` and `PRAGMA foreign_key_check;` during scheduled maintenance windows.
- **Crash Recovery & Idempotent Replay:** Every ingested file is recorded with its SHA-256 content hash in `ingestion_files` within the same transaction that inserts its records. If a power failure or `SIGKILL` interrupts ingestion, SQLite rolls back the uncommitted transaction completely. Upon restart, re-running ingestion cleanly processes the interrupted file without duplicate records or counter inflation.

---

## 2. Cryptographic Lifecycle & Key Management

### Cryptographic Invariants
- **Algorithm:** AES-256-GCM authenticated encryption with a unique 96-bit random nonce (`os.urandom(12)`) per encryption operation.
- **Tamper Resistance via AAD:** Authenticated Associated Data binds ciphertext to the immutable relational identity:
  $$\text{AAD} = \text{utf-8}(f"\{\text{workflow\_id}\}:\{\text{field\_name}\}:\{\text{phone\_lookup\_hmac}\}")$$
  Any attempt to transplant ciphertexts across records or fields fails authentication (`InvalidTag`).
- **Deterministic Tokenization:** HMAC-SHA256 of canonical phone (`+91XXXXXXXXXX`) with a dedicated 256-bit secret key distinct from the AES key. Eliminates plaintext phone storage while resisting offline rainbow table precomputation.

### Fail-Closed Key Policy
- **Missing Key Protection:** In `solution/cli.py` (`ingest` and `report`), if an existing database is detected but the key file (`.local/keys.json`) is absent, the pipeline fails immediately with `FileNotFoundError`. The system **never** silently generates new keys for an existing database.
- **Key Storage & Secrets Management:** For production deployment:
  - Local filesystem storage (`.local/keys.json`) is restricted to local development and test environments.
  - Production deployments must integrate with a hardware security module or managed KMS (AWS KMS, GCP Secret Manager, or HashiCorp Vault), injecting keys into memory via environment variables or runtime sidecars without writing raw key files to disk.
- **Key Rotation Strategy:**
  - *HMAC Lookup Key:* Rotation requires a full batch table update, re-computing HMACs for all canonical phones and re-indexing `leads` and `interaction_events`.
  - *AES Encryption Key:* Re-encryption phase reads existing ciphertext using Key Version $N$, decrypts in-memory, re-encrypts with Key Version $N+1$, and atomically commits the updated rows.

---

## 3. Data Quality & Operational Telemetry

### Monitoring & Threshold Alerting
The pipeline outputs comprehensive quality metrics in `data_quality.json`. In production, these metrics should be emitted to Prometheus or Datadog with alerting thresholds:
1. **Lead Rejection Rate:** `lead_rows_rejected / lead_rows_total > 5%` triggers an operational alert indicating upstream schema or CSV formatting anomalies.
2. **Field Warning Velocity:** A spike in `field_warnings` indicates date or currency format drift from client data feeds.
3. **Unresolved Orphan Backlog:** `orphan_events` monitors call events awaiting lead uploads. A sustained backlog older than 72 hours triggers an investigation into missing client batch deliveries.
4. **Unknown Disposition Anomaly:** Any non-zero count of `unknown_disposition_events` alerts operations to new disposition codes missing from `dispositions.csv`.
5. **Reconciliation Invariant Guard:** Automated morning validation verifies that for every batch:
   $$\text{total\_leads} = \text{yet\_to\_reach\_out} + \text{in\_progress\_telephony\_failed} + \text{in\_progress\_dialed} + \text{completed}$$

---

## 4. Scaling Architecture (Roadmap to 100k+ Leads/Day)

```
[ Client CSV Uploads ]        [ Telephony Event Webhooks ]
           |                                  |
           v                                  v
    [ Ingest API ]                     [ Event Buffer ]
           |                            (Kafka / SQS)
           v                                  v
+-------------------------------------------------------+
|            DISTRIBUTED PIPELINE WORKERS               |
|  - Lead Validation, HMAC Tokenization, AES-GCM Enc   |
|  - Event Deduplication & Sibling Override Linking     |
+-------------------------------------------------------+
                           |
                           v
+-------------------------------------------------------+
|             POSTGRESQL ENTERPRISE CLUSTER             |
|  - Partitioned tables: interaction_events (by month)  |
|  - Read Replicas for morning client report queries    |
|  - Connection Pooling (PgBouncer)                     |
+-------------------------------------------------------+
                           |
                           v
+-------------------------------------------------------+
|             REPORTING & METRICS ENGINE                |
|  - Materialized Views / Compiled SQL Aggregations     |
|  - S3 / GCS Export for client dashboards              |
+-------------------------------------------------------+
```

### Transition to PostgreSQL
1. **Connection Pooling & Concurrency:** Replace SQLite with PostgreSQL 16+ using PgBouncer to support high-throughput concurrent writes from multiple worker pods.
2. **Table Partitioning:** Partition `interaction_events` range-by-month on `occurred_at_utc`. This keeps index working sets in memory and accelerates daily reporting queries.
3. **Prepared Batch Ingestion (`executemany`):** Replace single-row queries with batch prepared statements, reducing network round-trips and achieving ingestion throughput exceeding 20,000 events/second.
4. **SQL Materialized Views:** Transition report calculations from Python memory into compiled SQL views or DBT models, taking advantage of database query parallelism.

---

## 5. Comprehensive Failure Modes & Mitigation Matrix

| Failure Scenario | Detection Mechanism | Prevention & Mitigation Strategy | Recovery & Test Validation |
|---|---|---|---|
| **Database File Crash / Power Loss** | Operating system exit / SQLite WAL lock | Atomic transactions (`BEGIN IMMEDIATE`); uncommitted transactions roll back. | Database auto-recovers WAL journal on reconnect; verified in `test_interrupted_ingestion_and_retry`. |
| **Duplicate Delivery Across Drops** | Unique constraint on `event_id` | Global deduplication check skips seen event IDs, increments `duplicate_events_dropped`. | Verified in `test_duplicate_events_across_drops`. |
| **Events Arriving Before Leads** | Foreign key check fails (`lead_id IS NULL`) | Preserves event in append-only ledger with `is_orphan = 1`. | Automated delayed orphan resolution on lead ingestion; verified in `test_delayed_orphan_resolution`. |
| **Supervisor Override Without Phone** | Absence of top-level `phone` key | Resolves lead association through sibling events sharing `call_id`. | Tested in `test_bucket_precedence_and_effective_disposition`. |
| **Client Modifies Ingested CSV** | SHA-256 hash mismatch on `relative_path` | `check_file_status` raises `ValueError` refusing to overwrite. | Verified in `test_idempotency_same_path_changed_content`. |
| **Tampered Ciphertext / AAD Mismatch** | AES-GCM tag verification | Fails cryptographic authentication (`InvalidTag`), refusing to return corrupted plaintext. | Verified in `test_aes_gcm_tampering_and_aad_mismatch`. |
| **Missing Encryption Keys** | Filesystem check in `cli.py` | Fail-closed policy refuses to generate new keys if database exists. | Verified in `test_missing_key_file_fail_closed`. |
| **Adversarial Phone in Event Payload** | Active payload key inspection | Key-level sanitization replaces any `"phone"` keys with `"[REDACTED]"`. | Verified in `test_persisted_event_privacy`. |
