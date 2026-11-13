"""
Database layer for SQLite.
Manages connections, migrations, WAL mode, relational constraints, and atomic transactions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_DB_PATH = Path(".local/data.db")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- Ingestion file receipts for idempotency
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

-- Master lead table
CREATE TABLE IF NOT EXISTS leads (
    lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_uuid TEXT NOT NULL UNIQUE,      -- Immutable internal lead UUID for AEAD context
    workflow_id TEXT NOT NULL,
    phone_lookup_hmac TEXT NOT NULL,     -- Deterministic lookup token
    encrypted_phone TEXT NOT NULL,
    encrypted_customer_name TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    initial_batch_id TEXT NOT NULL,
    policy_expiry TEXT,
    premium_amount TEXT,
    order_date TEXT,
    product TEXT,
    preferred_language TEXT NOT NULL,
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
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_raw TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    occurred_at_ist TEXT NOT NULL,
    phone_lookup_hmac TEXT,
    lead_id INTEGER REFERENCES leads(lead_id) ON DELETE SET NULL,
    is_orphan INTEGER NOT NULL DEFAULT 0,
    is_unknown_disposition INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_call ON interaction_events(call_id);
CREATE INDEX IF NOT EXISTS idx_events_lead ON interaction_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_lookup ON interaction_events(workflow_id, phone_lookup_hmac);
CREATE INDEX IF NOT EXISTS idx_events_orphan ON interaction_events(is_orphan);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON interaction_events(occurred_at_utc);

-- Global data quality metrics ledger
CREATE TABLE IF NOT EXISTS quality_metrics (
    metric_key TEXT PRIMARY KEY,
    metric_value INTEGER NOT NULL DEFAULT 0
);
"""


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Connect to SQLite with WAL mode, foreign keys, and Row factory."""
    p = Path(db_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Initialize schema tables and indexes."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def check_file_status(conn: sqlite3.Connection, relative_path: str, content_hash: str) -> Tuple[bool, bool]:
    """
    Check if a logical source file has been ingested.
    File identity is preserved by its logical path (drop + relative file path).
    Returns: (is_already_ingested, is_content_changed)
    - If previously ingested at this path with the exact same content hash: (True, False)
    - If previously ingested at this path with different content hash: (False, True)
    - If not previously ingested at this path: (False, False)
    """
    row = conn.execute(
        "SELECT content_hash FROM ingestion_files WHERE relative_path = ?",
        (relative_path,)
    ).fetchone()

    if row is not None:
        if row["content_hash"] == content_hash:
            return True, False
        return False, True

    return False, False


def record_file_ingestion(
    conn: sqlite3.Connection,
    drop_name: str,
    file_type: str,
    workflow_id: Optional[str],
    batch_id: Optional[str],
    relative_path: str,
    content_hash: str,
) -> None:
    """Record receipt of a successfully ingested file."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO ingestion_files (
            drop_name, file_type, workflow_id, batch_id, relative_path, content_hash, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (drop_name, file_type, workflow_id, batch_id, relative_path, content_hash, now_iso)
    )


def increment_metric(conn: sqlite3.Connection, metric_key: str, amount: int = 1) -> None:
    """Atomically increment a persistent quality metric."""
    conn.execute(
        """
        INSERT INTO quality_metrics (metric_key, metric_value)
        VALUES (?, ?)
        ON CONFLICT(metric_key) DO UPDATE SET metric_value = metric_value + ?
        """,
        (metric_key, amount, amount)
    )


def get_metric(conn: sqlite3.Connection, metric_key: str) -> int:
    """Retrieve value of a persistent metric (defaults to 0)."""
    row = conn.execute(
        "SELECT metric_value FROM quality_metrics WHERE metric_key = ?",
        (metric_key,)
    ).fetchone()
    return row["metric_value"] if row else 0
