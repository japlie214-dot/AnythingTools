# scripts/backfill_content_hashes.py
"""Standalone maintenance job: backfill empty content_hash columns.

This is the canonical home for content-hash completion logic, replacing
the broken SyncEngine._backfill_content_hashes call (which was both
undefined AND invoked on a read-only connection).

Usage:
    python -m scripts.backfill_content_hashes                  # all registered tables
    python -m scripts.backfill_content_hashes --table scraped_articles
    python -m scripts.backfill_content_hashes --table sf_quarterly_facts --dry-run

Design:
    - Reuses the filler registry in database/schemas/column_defaults.py
      (the SAME registry used by the schema migration system).
    - Routes all writes through database.writer.enqueue_transaction
      to honor the Single Writer invariant (README §4 Invariants:
      "All writes to operational databases MUST pass through the
      database.writer or database.logs_writer queues.")
    - Idempotent: only touches rows where content_hash = '' OR IS NULL.
    - Honors DATABASE_INTEGRATION_ENABLED=false (no-op when disabled).

Hash computation:
    The hash is SHA256 of the checksum columns (excluding embedding and
    vec_rowid) joined with "||". This is byte-for-byte identical to the
    computation in database/schemas/column_defaults._fill_content_hash
    (line 58: hashlib.sha256(concat.encode("utf-8")).hexdigest()) and
    to the hash computed at write time by:
      - database/articles/store.py:71 (ContentHasher.compute_row_hash)
      - tools/stock_financials/extractor.py:230 (compute_fact_hash)
      - tools/stock_notes/extractor.py:117,192 (filing_hash, note_hash)

References:
    - SQLite PRAGMA table_info: https://www.sqlite.org/pragma.html#pragma_table_info
    - SQLite SHA256: not built-in; computed in Python per the above.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
from typing import Iterable

from database.connection import DatabaseManager
from database.schemas.column_defaults import _REGISTRY
from database.backup.schema_registry import BackupSchemaRegistry
from database.backup.sync.helpers import introspect_table_columns
from database.writer import enqueue_transaction, wait_for_writes
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


def _iter_target_tables(only: str | None) -> Iterable[tuple[str, str]]:
    """Yield (table_name, column_name) pairs from the filler registry.

    The registry is keyed by table_name (lowercased) → {column_name: func}.
    We only emit entries where column_name == 'content_hash' (the registered
    fillers also cover embedding_status, which is out of scope here).
    """
    if only:
        only_lower = only.lower()
        if only_lower not in _REGISTRY:
            return
        for col in _REGISTRY[only_lower]:
            if col == "content_hash":
                yield only_lower, col
        return
    for tbl, cols in _REGISTRY.items():
        for col in cols:
            if col == "content_hash":
                yield tbl, col


def backfill_table(
    table_name: str,
    column_name: str = "content_hash",
    *,
    dry_run: bool = False,
) -> int:
    """Backfill content_hash for one table via the writer queue.

    Returns the number of rows that would be / were updated.

    The hash computation here is byte-for-byte identical to
    column_defaults._fill_content_hash — both use
    hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest().
    """
    # Use a read connection to find the rows needing backfill.
    # Per database/connection.py:142, get_read_connection() sets
    # PRAGMA query_only = ON — we cannot write through this conn.
    # We queue individual UPDATEs via enqueue_transaction instead.
    op_conn = DatabaseManager.get_read_connection()

    checksum_cols = BackupSchemaRegistry.get_checksum_columns(table_name)
    if not checksum_cols:
        log.dual_log(
            tag="Backfill:NoChecksumCols",
            level="INFO",
            message=f"Skipping {table_name}: no checksum columns registered",
            payload={"table": table_name},
        )
        return 0

    pk_col, _, _ = introspect_table_columns(op_conn, table_name)
    if not pk_col:
        log.dual_log(
            tag="Backfill:NoPK",
            level="WARNING",
            message=f"Skipping {table_name}: could not introspect PK",
            payload={"table": table_name},
        )
        return 0

    # Handle composite PKs (pk_col is a list). The UPDATE WHERE clause
    # must match on all PK columns.
    if isinstance(pk_col, (list, tuple)):
        pk_select = ", ".join(pk_col)
        where_pk = " AND ".join([f"{c} = ?" for c in pk_col])
    else:
        pk_select = pk_col
        where_pk = f"{pk_col} = ?"

    try:
        cursor = op_conn.execute(
            f"SELECT {pk_select}, {', '.join(checksum_cols)} FROM {table_name} "
            f"WHERE {column_name} = '' OR {column_name} IS NULL"
        )
    except Exception as e:
        log.dual_log(
            tag="Backfill:SelectFailed",
            level="ERROR",
            message=f"Could not query {table_name} for empty hashes: {e}",
            payload={"table": table_name, "error": str(e)},
            exc_info=e,
        )
        return 0

    total = 0
    batch: list[tuple[str, tuple]] = []
    BATCH_SIZE = 1000

    while True:
        rows = cursor.fetchmany(BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            if isinstance(pk_col, (list, tuple)):
                pk_vals = row[:len(pk_col)]
            else:
                pk_vals = (row[0],)
            # Hash computation: byte-for-byte identical to
            # column_defaults._fill_content_hash line 56-58.
            parts = [str(v or "").strip() for v in row[len(pk_vals):]]
            concat = "||".join(parts)
            new_hash = hashlib.sha256(concat.encode("utf-8")).hexdigest()
            if dry_run:
                total += 1
                continue
            batch.append((
                f"UPDATE {table_name} SET {column_name} = ? WHERE {where_pk}",
                (new_hash, *pk_vals),
            ))
        if batch and not dry_run:
            enqueue_transaction(batch, track=False)
            total += len(batch)
            batch = []

    if batch and not dry_run:
        enqueue_transaction(batch, track=False)
        total += len(batch)

    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--table",
        default=None,
        help="Specific table to backfill (default: all registered)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count rows without writing",
    )
    args = parser.parse_args()

    import config
    if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
        print("[INFO] DATABASE_INTEGRATION_ENABLED=false — nothing to do.")
        return 0

    start = time.time()
    grand_total = 0
    for table_name, column_name in _iter_target_tables(args.table):
        count = backfill_table(table_name, column_name, dry_run=args.dry_run)
        grand_total += count
        log.dual_log(
            tag="Backfill:Table",
            level="INFO",
            message=(
                f"{'Would update' if args.dry_run else 'Updated'} {count} rows "
                f"in {table_name}.{column_name}"
            ),
            payload={
                "table": table_name,
                "column": column_name,
                "rows": count,
                "dry_run": args.dry_run,
            },
        )

    if not args.dry_run:
        asyncio.run(wait_for_writes(timeout=120.0))

    elapsed = time.time() - start
    log.dual_log(
        tag="Backfill:Complete",
        level="INFO",
        message=(
            f"Backfill {'(dry-run) ' if args.dry_run else ''}complete: "
            f"{grand_total} rows in {elapsed:.2f}s"
        ),
        payload={
            "total_rows": grand_total,
            "elapsed_s": elapsed,
            "dry_run": args.dry_run,
        },
    )
    print(
        f"[OK] {'Would update' if args.dry_run else 'Updated'} "
        f"{grand_total} rows in {elapsed:.2f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
