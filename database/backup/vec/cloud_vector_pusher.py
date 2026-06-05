# database/backup/vec/cloud_vector_pusher.py
import json
import struct
from typing import List, Dict, Any
from sqlalchemy import text
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

VECTOR_DIM = 1024
VECTOR_TYPE = f"VECTOR(FLOAT, {VECTOR_DIM})"

class CloudVectorPusher:
    def __init__(self, circuit_breaker=None):
        self.circuit_breaker = circuit_breaker

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
        for batch_start in range(0, len(valid_rows), batch_size):
            batch = valid_rows[batch_start:batch_start + batch_size]
            pushed = self._push_batch(cloud_conn, schema, table_name, columns, batch, pk_col)
            total_pushed += pushed

        return {"pushed": total_pushed, "dlq": len(dlq_rows)}

    def _validate_and_normalize(self, rows: List[Dict]) -> tuple:
        valid_rows, dlq_rows = [], []
        for row in rows:
            embedding = row.get("embedding")
            if embedding is None:
                dlq_rows.append({**row, "_error_msg": "NULL embedding"})
                continue

            if isinstance(embedding, bytes):
                if len(embedding) != VECTOR_DIM * 4:
                    dlq_rows.append({**row, "_error_msg": f"Invalid BLOB length: {len(embedding)} bytes"})
                    continue
                float_list = list(struct.unpack(f'<{VECTOR_DIM}f', embedding))
                row["embedding"] = json.dumps(float_list)
            elif isinstance(embedding, str):
                try:
                    parsed = json.loads(embedding)
                    if not isinstance(parsed, list) or len(parsed) != VECTOR_DIM:
                        dlq_rows.append({**row, "_error_msg": "Invalid vector dimensions"})
                        continue
                except Exception as e:
                    dlq_rows.append({**row, "_error_msg": f"Invalid JSON embedding: {e}"})
                    continue
            elif isinstance(embedding, list):
                if len(embedding) != VECTOR_DIM:
                    dlq_rows.append({**row, "_error_msg": "Invalid vector dimensions"})
                    continue
                row["embedding"] = json.dumps(embedding)
            else:
                dlq_rows.append({**row, "_error_msg": "Unexpected embedding type"})
                continue
            valid_rows.append(row)
        return valid_rows, dlq_rows

    def _push_batch(self, cloud_conn, schema: str, table_name: str, columns: List[str], batch: List[Dict], pk_col: str) -> int:
        stage_table = f"{table_name}_stage"
        
        stage_col_defs = []
        for col in columns:
            if col == "embedding":
                stage_col_defs.append(f"{col} ARRAY")
            elif col == "rowid" or col == pk_col:
                stage_col_defs.append(f"{col} NUMBER")
            else:
                stage_col_defs.append(f"{col} VARCHAR")

        stage_ddl = f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({', '.join(stage_col_defs)})"
        cloud_conn.execute(text(stage_ddl))

        placeholders = [f"PARSE_JSON(:{c})" if c == "embedding" else f":{c}" for c in columns]
        insert_sql = f"INSERT INTO {schema}.{stage_table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
        cloud_conn.execute(text(insert_sql), batch)

        non_pk_cols = [c for c in columns if c != pk_col]
        update_sets = ", ".join([f"t.{c} = CASE WHEN s.{c} IS NOT NULL THEN s.{c}{'::' + VECTOR_TYPE if c == 'embedding' else ''} END" for c in non_pk_cols])
        insert_select = ", ".join([f"s.{c}::{VECTOR_TYPE}" if c == "embedding" else f"s.{c}" for c in columns])

        merge_sql = f"""
        MERGE INTO {schema}.{table_name} t
        USING {schema}.{stage_table} s
        ON t.{pk_col} = s.{pk_col}
        WHEN MATCHED THEN UPDATE SET {update_sets}
        WHEN NOT MATCHED THEN INSERT ({", ".join(columns)})
            SELECT {insert_select}
        """
        res = cloud_conn.execute(text(merge_sql))
        return res.rowcount if hasattr(res, 'rowcount') else len(batch)

    def _route_to_dlq(self, dlq_rows: List[Dict], table_name: str, pk_col: str):
        from database.backup.writer.backup_writer import BackupWriteTask, enqueue_backup_write
        for row in dlq_rows:
            enqueue_backup_write(BackupWriteTask(
                "dead_letter_queue", "DLQ",
                [{pk_col: row.get(pk_col, ""), "table_name": table_name,
                  "row_data": json.dumps({k: v for k, v in row.items() if k != "_error_msg"}, default=str),
                  "_error_msg": row.get("_error_msg", "Unknown error")}],
                pk_col
            ))
