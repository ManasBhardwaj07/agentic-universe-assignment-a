"""
Benchmark runner for Assignment A (supports small and large datasets).
Measures runtime, peak memory, database size, and record counts.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

from solution.cli import main as cli_main
from solution.database import get_connection


def run_benchmark(scale: str = "large", seed: int = 42, out_dir: str = "data_bench", db_path: str = ".local/bench.db", keys_path: str = ".local/bench_keys.json"):
    data_p = Path(out_dir)
    db_p = Path(db_path)
    keys_p = Path(keys_path)
    reports_p = Path("reports_bench")

    # Cleanup prior bench state
    if db_p.exists():
        db_p.unlink(missing_ok=True)
    if keys_p.exists():
        keys_p.unlink(missing_ok=True)

    print(f"=== Starting Benchmark: Scale={scale}, Seed={seed} ===")

    # 1. Generate Data
    t0 = time.perf_counter()
    gen_cmd = [
        sys.executable,
        "assignment-a-data/tools/generate_data.py",
        "--seed", str(seed),
        "--scale", scale,
        "--out", str(data_p)
    ]
    print(f"Generating {scale} dataset...")
    subprocess.run(gen_cmd, check=True)
    t_gen = time.perf_counter() - t0
    print(f"Data generated in {t_gen:.2f}s")

    # Measure system specs
    import platform
    print(f"OS: {platform.system()} {platform.release()}")
    print(f"Python: {platform.python_version()}")

    # 2. Init
    tracemalloc.start()
    t_start_total = time.perf_counter()
    assert cli_main(["init", "--db", str(db_p), "--keys", str(keys_p)]) == 0

    # 3. Ingest Drop 1
    t_ingest_1_start = time.perf_counter()
    assert cli_main(["ingest", str(data_p / "drop_1"), "--db", str(db_p), "--keys", str(keys_p)]) == 0
    t_drop_1 = time.perf_counter() - t_ingest_1_start

    # 4. Ingest Drop 2
    t_ingest_2_start = time.perf_counter()
    assert cli_main(["ingest", str(data_p / "drop_2"), "--db", str(db_p), "--keys", str(keys_p)]) == 0
    t_drop_2 = time.perf_counter() - t_ingest_2_start

    t_ingest_total = t_drop_1 + t_drop_2

    # 5. Report Generation
    t_report_start = time.perf_counter()
    assert cli_main(["report", "--out", str(reports_p), "--db", str(db_p), "--keys", str(keys_p)]) == 0
    t_report = time.perf_counter() - t_report_start

    t_total = time.perf_counter() - t_start_total
    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Query counts & sizes
    conn = get_connection(db_p)
    lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    event_count = conn.execute("SELECT COUNT(*) FROM interaction_events").fetchone()[0]
    orphan_count = conn.execute("SELECT COUNT(*) FROM interaction_events WHERE is_orphan = 1").fetchone()[0]
    conn.close()

    db_size_mb = db_p.stat().st_size / (1024 * 1024) if db_p.exists() else 0

    print("\n=== Benchmark Results ===")
    print(f"Lead Ingestion (Drop 1 + Drop 2): {t_ingest_total:.2f}s (Drop 1: {t_drop_1:.2f}s, Drop 2: {t_drop_2:.2f}s)")
    print(f"Report Generation: {t_report:.2f}s")
    print(f"Total Pipeline Runtime: {t_total:.2f}s")
    print(f"Peak Memory: {peak_mem / (1024 * 1024):.2f} MB")
    print(f"Leads Stored: {lead_count:,}")
    print(f"Events Stored: {event_count:,} (Final Orphans: {orphan_count:,})")
    print(f"Database File Size: {db_size_mb:.2f} MB")
    print("=========================\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", choices=["small", "large"], default="small")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    run_benchmark(scale=args.scale, seed=args.seed)
