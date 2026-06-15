# database/backup/vec/cloud_vector_pusher.py
import json
import struct
import math
from typing import List, Dict, Any, Tuple, Optional
from sqlalchemy import text
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

VECTOR_DIM = 1024
VECTOR_TYPE = f"VECTOR(FLOAT, {VECTOR_DIM})"
VECTOR_BYTES = VECTOR_DIM * 4

class VectorValidationError(Exception):
    pass

class VectorSync:
    def __init__(self, circuit_breaker=None):
        self.circuit_breaker = circuit_breaker

    @staticmethod
    def validate_vector(values: List[float], expected_dim: int = VECTOR_DIM) -> None:
        if not isinstance(values, list):
            raise VectorValidationError(f"Expected list, got {type(values).__name__}")
        if len(values) != expected_dim:
            raise VectorValidationError(f"Dimension mismatch: expected {expected_dim}, got {len(values)}")
        bad_indices = [i for i, x in enumerate(values) if not isinstance(x, (int, float)) or math.isnan(x) or math.isinf(x)]
        if bad_indices:
            raise VectorValidationError(f"Vector contains non-finite values at indices: {bad_indices[:10]}")

    @staticmethod
    def blob_to_float_list(blob: Optional[bytes]) -> Optional[List[float]]:
        if blob is None:
            return None
        if not isinstance(blob, bytes):
            raise VectorValidationError(f"Expected bytes, got {type(blob).__name__}")
        if len(blob) != VECTOR_BYTES:
            raise VectorValidationError(f"BLOB length mismatch: expected {VECTOR_BYTES} bytes, got {len(blob)} bytes")
        return list(struct.unpack(f'<{VECTOR_DIM}f', blob))

    @staticmethod
    def float_list_to_blob(values: List[float]) -> bytes:
        VectorSync.validate_vector(values)
        return struct.pack(f'<{VECTOR_DIM}f', *values)

    def push_vectors(
        self, cloud_conn, schema: str, table_name: str, columns: List[str], 
        rows: List[Dict[str, Any]], pk_col: str, batch_size: int = 100
    ) -> Dict[str, int]:
        if not rows:
            return {"pushed": 0, "dlq": 0}

        valid_rows, dlq_rows = self._validate_and_normalize(rows)
        if dlq_rows:
            self._route_to_dlq(dlq_rows, table_name, pk_col)

        if not valid_rows:
            return {"pushed": 0, "dlq": len(dlq_rows)}

        total_pushed = 0
        safe_batch_size = min(batch_size, 250)
        for batch_start in range(0, len(valid_rows), safe_batch_size):
            batch = valid_rows[batch_start:batch_start + safe_batch_size]
            pushed = self._push_batch(cloud_conn, schema, table_name, columns, batch, pk_col)
            total_pushed += pushed

        return {"pushed": total_pushed, "dlq": len(dlq_rows)}

    def _validate_and_normalize(self, rows: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        valid_rows, dlq_rows = [], []
        for row in rows:
            embedding = row.get("embedding")
            try:
                if embedding is None:
                    row["embedding"] = None
                elif isinstance(embedding, bytes):
                    float_list = self.blob_to_float_list(embedding)
                    self.validate_vector(float_list)
                    row["embedding"] = json.dumps(float_list)
                elif isinstance(embedding, str):
                    parsed = json.loads(embedding)
                    self.validate_vector(parsed)
                    row["embedding"] = json.dumps(parsed)
                elif isinstance(embedding, list):
                    self.validate_vector(embedding)
                    row["embedding"] = json.dumps(embedding)
                else:
                    dlq_rows.append({**row, "_error_msg": f"Unexpected embedding type: {type(embedding).__name__}"})
                    continue
                valid_rows.append(row)
            except Exception as e:
                # Store the error and strip raw bytes to avoid JSON serialization crash
                error_row = {k: v for k, v in row.items() if not isinstance(v, bytes)}
                error_row["_error_msg"] = f"Vector validation failed: {e}"
                dlq_rows.append(error_row)
        return valid_rows, dlq_rows

    def _push_batch(self, cloud_conn, schema: str, table_name: str, columns: List[str], batch: List[Dict], pk_col: str) -> int:
        stage_table = f"{table_name}_stage"
        
        from database.backup.schema_registry import BackupSchemaRegistry
        expected_types = BackupSchemaRegistry.expected_snowflake_types(table_name)
        
        stage_col_defs = []
        for col in columns:
            if col == "embedding":
                stage_col_defs.append(f"{col} VARCHAR")
            elif col in ("rowid", pk_col):
                stage_col_defs.append(f"{col} NUMBER")
            else:
                stage_col_defs.append(f"{col} {expected_types.get(col.lower(), 'VARCHAR')}")

        stage_ddl = f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({', '.join(stage_col_defs)})"
        cloud_conn.execute(text(stage_ddl))

        placeholders = []
        for col in columns:
            if col == "embedding":
                placeholders.append(f":{col}")
            else:
                placeholders.append(f":{col}")

        insert_sql = f"INSERT INTO {schema}.{stage_table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        cloud_conn.execute(text(insert_sql), batch)

        non_pk_cols = [c for c in columns if c != pk_col]
        update_sets = ", ".join([
            f"t.{c} = CASE WHEN s.{c} IS NOT NULL THEN PARSE_JSON(s.{c})::{VECTOR_TYPE} END" if c == 'embedding'
            else f"t.{c} = s.{c}"
            for c in non_pk_cols
        ])
        insert_select = ", ".join([f"PARSE_JSON(s.{c})::{VECTOR_TYPE}" if c == "embedding" else f"s.{c}" for c in columns])

        merge_sql = f"""
        MERGE INTO {schema}.{table_name} t
        USING {schema}.{stage_table} s
        ON t.{pk_col} = s.{pk_col}
        WHEN MATCHED THEN UPDATE SET {update_sets}
        WHEN NOT MATCHED THEN INSERT ({", ".join(columns)})
            VALUES ({insert_select})
        """
        res = cloud_conn.execute(text(merge_sql))
        return res.rowcount if hasattr(res, 'rowcount') else len(batch)

    def pull_vectors_from_cloud(self, cloud_rows: List[Any], columns: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        embedding_col_index = columns.index("embedding") if "embedding" in columns else None
        valid_rows, dlq_rows = [], []

        for row in cloud_rows:
            row_dict = dict(zip(columns, row))
            if embedding_col_index is not None:
                embedding_val = row[embedding_col_index]
                if embedding_val is None:
                    row_dict["embedding"] = None
                elif isinstance(embedding_val, list):
                    try:
                        self.validate_vector(embedding_val)
                        row_dict["embedding"] = self.float_list_to_blob(embedding_val)
                    except VectorValidationError as e:
                        dlq_rows.append({**row_dict, "_error_msg": f"Cloud validation failed: {e}"})
                        continue
                else:
                    dlq_rows.append({**row_dict, "_error_msg": f"Unexpected cloud embedding type: {type(embedding_val).__name__}"})
                    continue
            valid_rows.append(row_dict)
        return valid_rows, dlq_rows

    def _route_to_dlq(self, dlq_rows: List[Dict], table_name: str, pk_col: str):
        from database.backup.writer.cloud_writer import CloudWriteTask as BackupWriteTask, enqueue_cloud_write as enqueue_backup_write
        for row in dlq_rows:
            safe_row = {k: v for k, v in row.items() if k != "_error_msg" and not isinstance(v, bytes)}
            enqueue_backup_write(BackupWriteTask(
                "dead_letter_queue", "DLQ",
                [{pk_col: row.get(pk_col, ""), "table_name": table_name,
                  "row_data": json.dumps(safe_row, default=str),
                  "_error_msg": row.get("_error_msg", "Unknown error")}],
                pk_col
            ))

