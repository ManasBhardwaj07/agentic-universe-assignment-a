"""
Command-line interface for the Interaction Ledger & Reporting Pipeline.
Supported commands:
  python -m solution.cli init [--db PATH] [--keys PATH]
  python -m solution.cli ingest <path> [--db PATH] [--keys PATH]
  python -m solution.cli report --out <dir> [--db PATH] [--keys PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

from solution.crypto import CryptoManager, DEFAULT_KEYS_PATH
from solution.database import DEFAULT_DB_PATH, get_connection, init_db
from solution.events import EventIngestor
from solution.leads import LeadIngestor
from solution.reporting import ReportGenerator
from solution.schemas import DEFAULT_SCHEMAS_DIR, SchemaRegistry


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize database tables and generate or load keys."""
    db_path = Path(args.db)
    keys_path = Path(args.keys)

    print(f"Initializing database at: {db_path}")
    init_db(db_path)

    print(f"Loading/generating cryptographic keys at: {keys_path}")
    CryptoManager.load_or_create(keys_path)

    print("Initialization complete.")
    return 0


def _discover_and_ingest_drop(
    drop_dir: Path,
    lead_ingestor: LeadIngestor,
    event_ingestor: EventIngestor,
    conn,
) -> None:
    """
    Ingest leads and events for a single drop in deterministic order:
    1. Leads: workflows sorted, CSV files sorted by filename.
    2. Events: JSONL files sorted by filename.
    """
    drop_name = drop_dir.name
    leads_dir = drop_dir / "leads"
    events_dir = drop_dir / "events"

    # Step 1: Leads
    if leads_dir.exists():
        workflow_dirs = sorted([d for d in leads_dir.iterdir() if d.is_dir()], key=lambda d: d.name)
        for wf_dir in workflow_dirs:
            wf_id = wf_dir.name
            csv_files = sorted([f for f in wf_dir.glob("*.csv") if f.is_file()], key=lambda f: f.name)
            for csv_file in csv_files:
                with conn:
                    processed = lead_ingestor.ingest_csv(csv_file, wf_id, drop_name)
                    if processed:
                        print(f"  [+] Ingested lead batch: {drop_name}/leads/{wf_id}/{csv_file.name}")
                    else:
                        print(f"  [.] Skipped duplicate lead batch: {drop_name}/leads/{wf_id}/{csv_file.name}")
                # Resolve any pending orphan events that now link to these newly added leads
                with conn:
                    event_ingestor.resolve_orphans()

    # Step 2: Events
    if events_dir.exists():
        jsonl_files = sorted([f for f in events_dir.glob("*.jsonl") if f.is_file()], key=lambda f: f.name)
        for jsonl_file in jsonl_files:
            with conn:
                processed = event_ingestor.ingest_jsonl(jsonl_file, drop_name)
                if processed:
                    print(f"  [+] Ingested events: {drop_name}/events/{jsonl_file.name}")
                else:
                    print(f"  [.] Skipped duplicate events: {drop_name}/events/{jsonl_file.name}")


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest leads and call events from a drop folder or parent folder."""
    target_path = Path(args.path)
    if not target_path.exists():
        print(f"Error: Path does not exist: {target_path}", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    keys_path = Path(args.keys)
    schemas_dir = Path(args.schemas)

    if not keys_path.exists():
        if db_path.exists():
            print(f"Error: Key file missing at {keys_path} for existing database {db_path}. Refusing to generate new keys.", file=sys.stderr)
            return 1
        crypto = CryptoManager.load_or_create(keys_path)
    else:
        crypto = CryptoManager.load(keys_path)

    init_db(db_path)
    registry = SchemaRegistry(schemas_dir)
    conn = get_connection(db_path)

    lead_ingestor = LeadIngestor(conn, crypto, registry)
    event_ingestor = EventIngestor(conn, crypto, registry)

    try:
        # Check if target_path is a single drop directory (contains 'leads' or 'events')
        if (target_path / "leads").exists() or (target_path / "events").exists():
            print(f"Ingesting drop: {target_path.name}")
            _discover_and_ingest_drop(target_path, lead_ingestor, event_ingestor, conn)
        elif target_path.is_dir():
            # Parent directory containing drops (e.g. data/ with drop_1, drop_2)
            sub_drops = sorted(
                [d for d in target_path.iterdir() if d.is_dir() and ((d / "leads").exists() or (d / "events").exists())],
                key=lambda d: d.name
            )
            if sub_drops:
                for drop_dir in sub_drops:
                    print(f"Ingesting drop: {drop_dir.name}")
                    _discover_and_ingest_drop(drop_dir, lead_ingestor, event_ingestor, conn)
            else:
                print(f"Warning: No valid drop directories found under {target_path}")
        else:
            print(f"Error: Target path must be a directory: {target_path}", file=sys.stderr)
            return 1

        print("Ingestion complete.")
        return 0
    finally:
        conn.close()


def cmd_report(args: argparse.Namespace) -> int:
    """Generate all five operational report files."""
    out_dir = Path(args.out)
    db_path = Path(args.db)
    keys_path = Path(args.keys)
    schemas_dir = Path(args.schemas)

    if not keys_path.exists():
        print(f"Error: Cryptographic key file missing at {keys_path}. Cannot generate reports requiring PII masking.", file=sys.stderr)
        return 1

    crypto = CryptoManager.load(keys_path)
    registry = SchemaRegistry(schemas_dir)
    conn = get_connection(db_path)

    try:
        generator = ReportGenerator(conn, crypto, registry)
        print(f"Generating reports into: {out_dir}")
        paths = generator.generate_all_reports(out_dir)
        for name, p in paths.items():
            print(f"  [+] Created {p.name}")
        print("Reports generated successfully.")
        return 0
    finally:
        conn.close()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solution",
        description="Agentic Universe - Interaction Ledger & Reporting Pipeline (Assignment A)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Initialize database and cryptographic keys")
    p_init.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    p_init.add_argument("--keys", default=str(DEFAULT_KEYS_PATH), help="Path to cryptographic keys JSON")
    p_init.set_defaults(func=cmd_init)

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest leads and events")
    p_ingest.add_argument("path", help="Path to drop directory or parent data directory")
    p_ingest.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    p_ingest.add_argument("--keys", default=str(DEFAULT_KEYS_PATH), help="Path to cryptographic keys JSON")
    p_ingest.add_argument("--schemas", default=str(DEFAULT_SCHEMAS_DIR), help="Path to schemas directory")
    p_ingest.set_defaults(func=cmd_ingest)

    # report
    p_report = subparsers.add_parser("report", help="Generate all 5 reports")
    p_report.add_argument("--out", required=True, help="Directory to write reports into")
    p_report.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to SQLite database")
    p_report.add_argument("--keys", default=str(DEFAULT_KEYS_PATH), help="Path to cryptographic keys JSON")
    p_report.add_argument("--schemas", default=str(DEFAULT_SCHEMAS_DIR), help="Path to schemas directory")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
