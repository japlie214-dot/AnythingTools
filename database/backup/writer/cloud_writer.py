# database/backup/writer/cloud_writer.py
"""
Cloud writer thread for best-effort inline Snowflake writes.

Replaces the old backup_writer.py which wrote to backup.db.
Now writes directly to Snowflake via CloudEngine's MERGE logic.

Design: Fire-and-forget queue. If Snowflake is unavailable or
the circuit breaker is open, writes are best-effort and will be retried
once with exponential backoff before being routed to the DLQ.
The periodic SyncEngine.sync_all() will catch any missed rows.
"""

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional, List, Any, Union, Tuple
from sqlalchemy import text

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


@dataclass
class CloudWriteTask:
    """A single row to be written to Snowflake."""
    table_name: str
    operation: str  # UPSERT, DELETE
    records: List[dict]
    pk_col: Union[str, Tuple[str, ...]] = "id"


# Global cloud write queue
cloud_write_queue: queue.Queue = queue.Queue(maxsize=10000)
_cloud_shutdown = threading.Event()
_cloud_writer_thread: Optional[threading.Thread] = None
_shared_cloud_engine: Optional[Any] = None
_owns_engine: bool = False


def start_cloud_writer(cloud_engine: Optional[Any] = None):
    """Start the cloud writer thread (idempotent).

    If a shared CloudEngine instance is provided it will be used by the
    background thread instead of creating its own. This allows the
    SyncEngine to pass a single pooled engine instance to avoid connection
    exhaustion.
    """
    global _cloud_writer_thread, _shared_cloud_engine
    if _cloud_writer_thread is not None and _cloud_writer_thread.is_alive():
        return
    
    _shared_cloud_engine = cloud_engine
    _cloud_shutdown.clear()
    _cloud_writer_thread = threading.Thread(
        target=_cloud_writer_loop,
        name="cloud-writer",
        daemon=True,
    )
    _cloud_writer_thread.start()
    log.dual_log(
        tag="Backup:Writer:Start",
        message="Cloud writer thread started",
        level="INFO",
        payload={"action": "start", "shared_engine": cloud_engine is not None}
    )


def enqueue_cloud_write(table_name: Any, row_data: Optional[dict] = None, pk_col: Union[str, Tuple[str, ...], List[str]] = "id"):
    """Enqueue a best-effort cloud write. Non-blocking.

    Supports polymorphic signature:
    - enqueue_cloud_write(task: CloudWriteTask)
    - enqueue_cloud_write(table_name: str, row_data: dict, pk_col: str = "id")
    """
    # Honor the master DB integration toggle. When disabled (e.g. for
    # testing), all cloud writes are silently skipped. This is the single
    # chokepoint for cloud writes.
    try:
        import config
        if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
            return
    except ImportError:
        pass

    if isinstance(pk_col, list):
        pk_col = tuple(pk_col)
    try:
        if isinstance(table_name, CloudWriteTask):
            cloud_write_queue.put_nowait(table_name)
        else:
            task = CloudWriteTask(table_name=table_name, operation="UPSERT", records=[row_data], pk_col=pk_col)
            cloud_write_queue.put_nowait(task)
    except queue.Full:
        target_name = getattr(table_name, 'table_name', table_name)
        # Promoted from DEBUG to WARNING: dropped writes are operationally
        # significant and must be visible in production logs. The payload
        # includes queue depth and capacity so operators can correlate
        # drops with throughput spikes.
        log.dual_log(
            tag="Backup:Writer:QueueFull",
            level="WARNING",
            message=f"Cloud write queue full, dropping write for {target_name}",
            payload={
                "table": target_name,
                "queue_size": cloud_write_queue.qsize(),
                "queue_max": cloud_write_queue.maxsize,
            },
        )


def enqueue_cloud_write_batch(table_name: str, records: list, pk_col: Union[str, Tuple[str, ...], List[str]] = "id"):
    """Enqueue a batch of records for best-effort cloud upsert."""
    try:
        import config
        if not getattr(config, "DATABASE_INTEGRATION_ENABLED", True):
            return
    except ImportError:
        pass

    if isinstance(pk_col, list):
        pk_col = tuple(pk_col)
    try:
        task = CloudWriteTask(table_name=table_name, operation="UPSERT", records=records, pk_col=pk_col)
        cloud_write_queue.put_nowait(task)
    except queue.Full:
        log.dual_log(
            tag="Backup:Writer:QueueFull",
            level="WARNING",
            message=f"Cloud write queue full, dropping batch for {table_name}",
            payload={
                "table": table_name,
                "batch_size": len(records),
                "queue_size": cloud_write_queue.qsize(),
                "queue_max": cloud_write_queue.maxsize,
            },
        )


def enqueue_cloud_delete(table_name: str, pk_val: Any, pk_col: Union[str, Tuple[str, ...], List[str]] = "id"):
    """Enqueue a best-effort cloud delete (by PK).

    Contract:
      - pk_val is a SCALAR (str, int, etc.) when pk_col is a str.
      - pk_val is a DICT (column_name -> value) when pk_col is a tuple of multiple
        column names (composite PK).

    Do NOT pass a dict when pk_col is a str — the dict will end up as a bound
    parameter and Snowflake's DBAPI rejects dict values with
    "Binding data in type (dict) is not supported".
    """
    if isinstance(pk_col, list):
        pk_col = tuple(pk_col)
    
    # Defense in depth: catch the dict-when-scalar mistake at enqueue time
    # rather than at flush time (which is async and harder to debug).
    if isinstance(pk_col, str) and isinstance(pk_val, dict):
        log.dual_log(
            tag="Backup:Writer:DeleteBadPayload",
            level="ERROR",
            message=f"enqueue_cloud_delete received a dict pk_val for scalar pk_col '{pk_col}' on {table_name}",
            payload={"table": table_name, "pk_col": pk_col, "pk_val_type": type(pk_val).__name__}
        )
        raise TypeError(
            f"enqueue_cloud_delete('{table_name}', pk_val=<{type(pk_val).__name__}>, pk_col='{pk_col}'): "
            f"pk_val must be a scalar when pk_col is a string. Pass the value directly, not a dict."
        )
        
    records = [pk_val] if isinstance(pk_col, tuple) else [{pk_col: pk_val}]
    try:
        task = CloudWriteTask(table_name=table_name, operation="DELETE", records=records, pk_col=pk_col)
        cloud_write_queue.put_nowait(task)
    except queue.Full:
        log.dual_log(
            tag="Backup:Writer:QueueFull",
            level="WARNING",
            message=f"Cloud write queue full, dropping delete for {table_name}",
            payload={
                "table": table_name,
                "pk": pk_val,
                "queue_size": cloud_write_queue.qsize(),
                "queue_max": cloud_write_queue.maxsize,
            },
        )


def _route_failed_batch_to_dlq(
    table_name: str,
    records: list,
    error_msg: str,
    pk_col: Union[str, Tuple[str, ...]] = "id",
):
    """Route failed records to the Dead Letter Queue with full observability.
    
    Args:
        table_name: The target table that failed.
        records: List of record dicts that failed to write.
        error_msg: The complete error message (never truncated).
        pk_col: Primary key column name(s). String for single PK,
            tuple for composite PK. Used to construct a stable
            row_id for DLQ deduplication and operator traceability.
    """
    from database.writer import enqueue_write
    from utils.id_generator import ULID
    import json as _json
    for record in records:
        try:
            record_dict = dict(record)
            safe_record = {
                k: v for k, v in record_dict.items()
                if not isinstance(v, (bytes, bytearray))
            }

            # Construct row_id from primary key column(s).
            # Resolution: composite PK join → single PK value →
            # 'id' fallback → 'rowid' fallback → MD5 hash.
            row_id_val = _build_row_id(pk_col, record_dict)

            enqueue_write(
                "INSERT OR IGNORE INTO dead_letter_queue (dlq_id, table_name, row_id, row_data, error_message) VALUES (?, ?, ?, ?, ?)",
                (ULID.generate(), table_name, row_id_val,
                 _json.dumps(safe_record, default=str), error_msg or "")
            )
        except Exception as e:
            log.dual_log(
                tag="Backup:Writer:DLQError",
                level="ERROR",
                message=f"Failed to route record to DLQ for {table_name}",
                payload={"table": table_name, "error": str(e), "record_count": len(records)}
            )


def _build_row_id(
    pk_col: Union[str, Tuple[str, ...]], record: dict
) -> str:
    """Construct a stable row identifier from PK columns and record data.
    
    Resolution order:
    1. Composite PK: join values with '|' delimiter
    2. Single PK: use the value directly
    3. Fallback to 'id' column
    4. Fallback to 'rowid' column
    5. MD5 hash of the safe record data
    """
    import hashlib
    import json
    
    if isinstance(pk_col, tuple) and len(pk_col) > 1:
        if all(k in record for k in pk_col):
            return "|".join(str(record[k]) for k in pk_col)
    if isinstance(pk_col, str) and pk_col in record:
        return str(record[pk_col])
    if "id" in record:
        return str(record["id"])
    if "rowid" in record:
        return str(record["rowid"])
    safe = {k: v for k, v in record.items() if not isinstance(v, (bytes, bytearray))}
    return hashlib.md5(json.dumps(safe, sort_keys=True, default=str).encode()).hexdigest()


def _flush_batch(cloud_engine, batch_buffer: dict, _retry_depth: int = 0):
    """Flush accumulated writes to Snowflake with a single retry/backoff and DLQ routing."""
    from database.backup.observability.metrics import BackupMetricsCollector

    RETRY_LIMIT = 1
    RETRY_DELAY_BASE = 2.0
    import time as _time

    for (table_name, operation, pk_col), records in batch_buffer.items():
        start_flush = _time.monotonic()
        try:
            with cloud_engine.engine.begin() as conn:
                schema = cloud_engine.settings.schema_name
                pk_cols = [pk_col] if isinstance(pk_col, str) else list(pk_col)

                if operation == "DELETE":
                    if isinstance(pk_col, str):
                        pk_vals = [r[pk_col] for r in records if pk_col in r]
                        if pk_vals:
                            placeholders = ",".join([f":p{i}" for i in range(len(pk_vals))])
                            params = {f"p{i}": val for i, val in enumerate(pk_vals)}
                            conn.execute(text(f"DELETE FROM {schema}.{table_name} WHERE {pk_col} IN ({placeholders})"), params)
                    else:
                        for r in records:
                            conds = " AND ".join([f"{c} = :{c}" for c in pk_cols])
                            params = {c: r[c] for c in pk_cols if c in r}
                            conn.execute(text(f"DELETE FROM {schema}.{table_name} WHERE {conds}"), params)
                    BackupMetricsCollector.record_flush(True)
                    continue

                if not records:
                    BackupMetricsCollector.record_flush(True)
                    continue

                from database.backup.engine.type_sanitizer import sanitize_snowflake_params
                records = sanitize_snowflake_params(records)

                columns = list(records[0].keys())
                
                # Deduplicate records by pk_cols (keep only the latest/newest record for each PK in this batch)
                deduped_records = {}
                for r in records:
                    if isinstance(pk_col, str):
                        pk_val = r.get(pk_col)
                    else:
                        pk_val = tuple(r.get(c) for c in pk_cols) if all(c in r for c in pk_cols) else None
                    if pk_val is not None:
                        deduped_records[pk_val] = r
                    else:
                        deduped_records[id(r)] = r
                records = list(deduped_records.values())

                has_embedding = "embedding" in columns

                if has_embedding:
                    # Use VectorSync for tables with embeddings
                    from database.backup.vec.cloud_vector_pusher import VectorSync
                    pusher = VectorSync(circuit_breaker=cloud_engine.circuit_breaker_vec)
                    try:
                        push_result = pusher.push_vectors(
                            conn, schema, table_name, columns, records, pk_col
                        )
                        pushed = push_result.get('pushed', 0) if isinstance(push_result, dict) else 0
                        log.dual_log(
                            tag="Backup:Writer:Flush",
                            message=f"Flushed {pushed} rows to {table_name}",
                            level="DEBUG",
                            payload={"table": table_name, "rows": pushed, "latency_ms": round((_time.monotonic() - start_flush) * 1000, 1)}
                        )
                        BackupMetricsCollector.record_flush(True)
                    except Exception as e:
                        log.dual_log(
                            tag="Backup:Writer:FlushError",
                            level="WARNING",
                            message=f"Failed to flush {table_name}",
                            payload={"table": table_name, "error": str(e)}
                        )
                        raise
                else:
                    # Standard MERGE for non-embedding tables
                    stage_table = f"{table_name}_stage"

                    from database.backup.schema_registry import BackupSchemaRegistry
                    expected_types = BackupSchemaRegistry.expected_snowflake_types(table_name)
                    col_defs = ",".join([f"{c} {expected_types.get(c.lower(), 'VARCHAR')}" for c in columns])
                    conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({col_defs})"))

                    insert_placeholders = ",".join([f":{c}" for c in columns])
                    conn.execute(text(f"INSERT INTO {schema}.{stage_table} VALUES ({insert_placeholders})"), records)

                    merge_on = " AND ".join([f"t.{c} = s.{c}" for c in pk_cols])
                    update_set = ", ".join([f"t.{c} = s.{c}" for c in columns if c not in pk_cols])
                    
                    merge_sql = f"""
                    MERGE INTO {schema}.{table_name} t
                    USING {schema}.{stage_table} s
                    ON {merge_on}
                    WHEN MATCHED THEN UPDATE SET {update_set}
                    WHEN NOT MATCHED THEN INSERT ({",".join(columns)}) VALUES ({",".join([f"s.{c}" for c in columns])})
                    """
                    result = conn.execute(text(merge_sql))

                    log.dual_log(
                        tag="Backup:Writer:Flush",
                        message=f"Flushed {len(records)} rows to Snowflake {table_name}",
                        level="DEBUG",
                        payload={"table": table_name, "rows": len(records), "latency_ms": round((_time.monotonic() - start_flush) * 1000, 1)}
                    )
                    BackupMetricsCollector.record_flush(True)

        except Exception as e:
            # Retry with exponential backoff once, then DLQ
            if _retry_depth < RETRY_LIMIT:
                delay = RETRY_DELAY_BASE * (2 ** _retry_depth)
                log.dual_log(
                    tag="Backup:Writer:Retry",
                    level="WARNING",
                    message=f"Batch flush failed for {table_name}, retrying in {delay}s (attempt {_retry_depth + 1}/{RETRY_LIMIT})",
                    payload={"table": table_name, "error": str(e), "retry_in": delay, "attempt": _retry_depth + 1}
                )
                BackupMetricsCollector.record_flush(False, retried=True)
                _time.sleep(delay)
                _flush_batch(cloud_engine, {(table_name, operation, pk_col): records}, _retry_depth=_retry_depth + 1)
            else:
                log.dual_log(
                    tag="Backup:Writer:BatchError",
                    level="ERROR",
                    message=f"Batch flush failed for {table_name} after {RETRY_LIMIT} retries",
                    payload={
                        "table": table_name, "error": str(e),
                        "retries_exhausted": True, "record_count": len(records),
                    }
                )
                BackupMetricsCollector.record_flush(False, dlq=True)
                _route_failed_batch_to_dlq(table_name, records, str(e), pk_col=pk_col)


def _cloud_writer_loop():
    """Background thread that drains the cloud write queue and writes to Snowflake."""
    global _shared_cloud_engine, _owns_engine
    cloud_engine = _shared_cloud_engine

    if cloud_engine is None:
        from database.backup.settings import BackupSettings
        try:
            settings = BackupSettings()
        except Exception as e:
            log.dual_log(
                tag="Backup:Writer:ConfigError",
                level="WARNING",
                message="Backup settings not configured, cloud writer inactive",
                payload={"error": str(e)}
            )
            return

        if not settings.cloud.enabled:
            log.dual_log(
                tag="Backup:Writer:Disabled",
                level="INFO",
                message="Cloud backup disabled, cloud writer thread idle",
                payload={"action": "check_enabled"}
            )
            return

        from database.backup.engine.cloud_engine import CloudEngine
        cloud_engine = CloudEngine(settings.cloud, settings.sync)
        _owns_engine = True
    else:
        _owns_engine = False
        log.dual_log(
            tag="Backup:Writer:SharedEngine",
            level="INFO",
            message="Using shared CloudEngine from SyncEngine",
            payload={"shared_engine": True}
        )

    batch_buffer = {}  # (table, op, pk_col) -> list of records
    last_flush = time.monotonic()
    FLUSH_INTERVAL = 5.0  # seconds
    MAX_BATCH_SIZE = 100  # records per flush

    try:
        while not _cloud_shutdown.is_set():
            try:
                task = cloud_write_queue.get(timeout=1.0)
                if task is None:
                    break

                key = (task.table_name, task.operation, task.pk_col)
                batch_buffer.setdefault(key, [])
                batch_buffer[key].extend(task.records)

            except queue.Empty:
                pass

            now = time.monotonic()
            should_flush = (
                any(len(v) >= MAX_BATCH_SIZE for v in batch_buffer.values()) or
                (now - last_flush >= FLUSH_INTERVAL and any(batch_buffer.values()))
            )

            if should_flush and batch_buffer:
                _flush_batch(cloud_engine, batch_buffer)
                batch_buffer = {}
                last_flush = now

        # Final flush on shutdown
        if batch_buffer:
            _flush_batch(cloud_engine, batch_buffer)

    finally:
        # Shutdown CloudEngine only if this thread owns it
        if _owns_engine and cloud_engine is not None:
            try:
                cloud_engine.shutdown()
                log.dual_log(tag="Backup:Writer:Shutdown", message="Cloud writer thread stopped (owned engine disposed)", level="INFO", payload={"action": "shutdown", "owned": True})
            except Exception as e:
                log.dual_log(tag="Backup:Writer:ShutdownError", message=f"Error shutting down CloudEngine: {e}", level="WARNING", payload={"error": str(e)})
        else:
            log.dual_log(tag="Backup:Writer:Shutdown", message="Cloud writer thread stopped (shared engine preserved)", level="INFO", payload={"action": "shutdown", "owned": False})
