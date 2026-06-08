"""database/backup/writer/cloud_writer.py
Cloud writer thread for best-effort inline Snowflake writes.

Replaces the old backup_writer.py which wrote to backup.db.
Now writes directly to Snowflake via CloudEngine's MERGE logic.

Design: Fire-and-forget queue. If Snowflake is unavailable or
the circuit breaker is open, writes are silently dropped.
The periodic SyncEngine.sync_all() will catch any missed rows.
"""

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional, List
from sqlalchemy import text

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


@dataclass
class CloudWriteTask:
    """A single row to be written to Snowflake."""
    table_name: str
    operation: str  # UPSERT, DELETE
    records: List[dict]
    pk_col: str = "id"


# Global cloud write queue
cloud_write_queue: queue.Queue = queue.Queue(maxsize=10000)
_cloud_shutdown = threading.Event()
_cloud_writer_thread: Optional[threading.Thread] = None


def start_cloud_writer():
    """Start the cloud writer thread (idempotent)."""
    global _cloud_writer_thread
    if _cloud_writer_thread is not None and _cloud_writer_thread.is_alive():
        return
    
    _cloud_shutdown.clear()
    _cloud_writer_thread = threading.Thread(
        target=_cloud_writer_loop,
        name="cloud-writer",
        daemon=True,
    )
    _cloud_writer_thread.start()
    log.dual_log(
        tag="CloudWriter:Started",
        message="Cloud writer thread started",
        level="INFO"
    )


def enqueue_cloud_write(table_name: str, row_data: dict, pk_col: str = "id"):
    """Enqueue a best-effort cloud write. Non-blocking.

    row_data: a mapping representing a single record
    pk_col: primary key column name for the target table in Snowflake
    """
    try:
        task = CloudWriteTask(table_name=table_name, operation="UPSERT", records=[row_data], pk_col=pk_col)
        cloud_write_queue.put_nowait(task)
    except queue.Full:
        log.dual_log(tag="CloudWriter:QueueFull", level="DEBUG", message=f"Queue full, dropping write for {table_name}", payload={"table": table_name})


def enqueue_cloud_write_batch(table_name: str, records: list, pk_col: str = "id"):
    """Enqueue a batch of records for best-effort cloud upsert."""
    try:
        task = CloudWriteTask(table_name=table_name, operation="UPSERT", records=records, pk_col=pk_col)
        cloud_write_queue.put_nowait(task)
    except queue.Full:
        log.dual_log(tag="CloudWriter:QueueFullBatch", level="DEBUG", message=f"Queue full, dropping batch for {table_name}", payload={"table": table_name, "batch_size": len(records)})


def enqueue_cloud_delete(table_name: str, pk_val: str, pk_col: str = "id"):
    """Enqueue a best-effort cloud delete (by PK)."""
    try:
        task = CloudWriteTask(table_name=table_name, operation="DELETE", records=[{pk_col: pk_val}], pk_col=pk_col)
        cloud_write_queue.put_nowait(task)
    except queue.Full:
        log.dual_log(tag="CloudWriter:QueueFullDelete", level="DEBUG", message=f"Queue full, dropping delete for {table_name}", payload={"table": table_name, "pk": pk_val})


def _cloud_writer_loop():
    """Background thread that drains the cloud write queue and writes to Snowflake."""
    from database.backup.settings import BackupSettings
    
    try:
        settings = BackupSettings()
    except Exception:
        log.dual_log(
            tag="CloudWriter:ConfigError",
            level="WARNING",
            message="Backup settings not configured, cloud writer inactive"
        )
        return
    
    if not settings.cloud.enabled:
        log.dual_log(
            tag="CloudWriter:Disabled",
            level="INFO",
            message="Cloud backup disabled, cloud writer thread idle"
        )
        return
    
    from database.backup.engine.cloud_engine import CloudEngine
    cloud_engine = CloudEngine(settings.cloud, settings.sync)
    
    batch_buffer = {}  # table_name -> list of records
    last_flush = time.monotonic()
    FLUSH_INTERVAL = 5.0  # seconds
    MAX_BATCH_SIZE = 100  # records per flush
    
    while not _cloud_shutdown.is_set():
        try:
            task = cloud_write_queue.get(timeout=1.0)
            if task is None:
                break
            
            # Accumulate into batch
            if task.table_name not in batch_buffer:
                batch_buffer[task.table_name] = []
            batch_buffer[task.table_name].extend(task.records)
            
        except queue.Empty:
            pass
        
        # Flush if batch is large enough or interval elapsed
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
    
    cloud_engine.shutdown()
    log.dual_log(tag="CloudWriter:Shutdown", message="Cloud writer thread stopped")


def _flush_batch(cloud_engine, batch_buffer: dict):
    """Flush accumulated writes to Snowflake."""
    for table_name, records in batch_buffer.items():
        try:
            with cloud_engine.engine.begin() as conn:
                schema = cloud_engine.settings.schema_name
                
                # Get columns from first record
                if not records:
                    continue
                columns = list(records[0].keys())
                
                # Handle embedding column
                has_embedding = "embedding" in columns
                if has_embedding:
                    # Use VectorSync for tables with embeddings
                    from database.backup.vec.cloud_vector_pusher import VectorSync
                    pusher = VectorSync(circuit_breaker=cloud_engine.circuit_breaker_vec)
                    try:
                        push_result = pusher.push_vectors(
                            conn, schema, table_name, columns, records, "id"
                        )
                        log.dual_log(
                            tag="CloudWriter:Flush",
                            message=f"Flushed {push_result.get('pushed', 0)} rows to {table_name}",
                            level="DEBUG",
                            payload={"table": table_name, "rows": push_result.get('pushed', 0)}
                        )
                    except Exception as e:
                        log.dual_log(
                            tag="CloudWriter:FlushError",
                            level="WARNING",
                            message=f"Failed to flush {table_name}: {e}",
                            payload={"table": table_name, "error": str(e)[:200]}
                        )
                else:
                    # Standard MERGE for non-embedding tables
                    stage_table = f"{table_name}_stage"
                    pk_col = "id"
                    
                    # Find PK column
                    for rec in records:
                        for k, v in rec.items():
                            if k == "id" or k.endswith("_id"):
                                pk_col = k
                                break
                        if pk_col != "id":
                            break
                    
                    col_defs = ",".join([f"{c} VARCHAR" for c in columns])
                    conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({col_defs})"))
                    
                    insert_placeholders = ",".join([f":{c}" for c in columns])
                    conn.execute(text(f"INSERT INTO {schema}.{stage_table} VALUES ({insert_placeholders})"), records)
                    
                    merge_sql = f"""
                    MERGE INTO {schema}.{table_name} t
                    USING {schema}.{stage_table} s
                    ON t.{pk_col} = s.{pk_col}
                    WHEN MATCHED THEN UPDATE SET {", ".join([f"t.{c} = s.{c}" for c in columns if c != pk_col])}
                    WHEN NOT MATCHED THEN INSERT ({",".join(columns)}) VALUES ({",".join([f"s.{c}" for c in columns])})
                    """
                    result = conn.execute(text(merge_sql))
                    
                    log.dual_log(
                        tag="CloudWriter:Flush",
                        message=f"Flushed {len(records)} rows to Snowflake {table_name}",
                        level="DEBUG",
                        payload={"table": table_name, "rows": len(records)}
                    )
        except Exception as e:
            log.dual_log(
                tag="CloudWriter:BatchError",
                level="WARNING",
                message=f"Batch flush failed for {table_name}: {e}",
                payload={"table": table_name, "error": str(e)[:200]}
            )