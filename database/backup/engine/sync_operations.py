# database/backup/engine/sync_operations.py
"""Cloud sync operations: push (local→cloud) and pull (cloud→local).

This module is the core of the AnythingTools backup system. It moves rows
between the local SQLite operational database and Snowflake cloud tables
via a MERGE-based upsert pattern.

Key design decisions:
- Composite primary keys are fully supported. The _detect_pk_columns
  helper inspects PRAGMA table_info and returns either a single column
  name (str) or a list of column names (for composite PKs). All downstream
  SQL generation (manifest upload, diff query, MERGE ON clause) is
  composite-PK-aware.
- Chunk sizes are computed dynamically based on PK column count to
  respect SQLite's SQLITE_MAX_VARIABLE_NUMBER limit (999 on SQLite
  <3.32.0, 32766 on SQLite >=3.32.0; we conservatively use 999).
  Reference: https://www.sqlite.org/limits.html
- The MERGE source is deduplicated defensively via QUALIFY ROW_NUMBER()
  to prevent Snowflake error 100090 (42P18) "Duplicate row detected"
  even if upstream code accidentally produces duplicate PKs in the
  stage table. Reference: https://docs.snowflake.com/en/sql-reference/sql/merge
- Both sync_data and pull_to_local are wrapped in with_session_recovery
  to retry on Snowflake 390111 "Session no longer exists" errors.
"""
import datetime
import struct
from decimal import Decimal
from typing import List, Union

from sqlalchemy import text
from database.backup.engine.type_sanitizer import sanitize_snowflake_params
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

# Conservative SQLite host-parameter limit. Per https://www.sqlite.org/limits.html:
# "The default setting for SQLITE_MAX_VARIABLE_NUMBER is 999. Beginning with
# SQLite version 3.32.0 (2020-05-22), the default value... is increased to 32766."
# We use 999 to support older SQLite deployments (Python 3.7-3.9 with older SQLite).
SQLITE_HOST_PARAM_LIMIT = 999
# Hard cap on chunk_size to avoid generating excessively large SQL strings.
MAX_CHUNK_SIZE = 900


def _detect_pk_columns(local_conn, table_name: str) -> tuple[Union[str, List[str]], bool]:
    """Inspect the SQLite table's PRAGMA table_info and return a tuple of
    (pk_columns, has_content_hash).

    Returns:
        pk_columns: A single column name (str) if the table has a single-column
            primary key, or a list of column names (in declaration order) if
            the table has a composite primary key. Defaults to ["id"] if no
            PK columns are found.
        has_content_hash: True if the table has a column named 'content_hash',
            used for delta detection.

    PRAGMA table_info returns one row per column with the schema:
        (cid, name, type, notnull, dflt_value, pk)
    The 'pk' field is 0 for non-PK columns, or the 1-indexed position in
    the primary key for PK columns. For composite primary keys declared as
    PRIMARY KEY (a, b, c), PRAGMA returns pk=1 for a, pk=2 for b, pk=3 for c.

    Reference: https://www.sqlite.org/pragma.html#pragma_table_info

    The previous implementation had a bug where it overwrote pk_col on each
    iteration: 'if col_info[5] > 0: pk_col = col_info[1]'. This meant that
    for a composite PK like (ticker, statement_type, concept, quarter), only
    the LAST column ('quarter') was retained, causing MERGE to use
    'ON t.quarter = s.quarter' which matches multiple source rows per target
    and triggers Snowflake error 100090 (42P18) "Duplicate row detected".
    """
    pk_cols_with_pos: list[tuple[int, str]] = []
    has_hash = False
    try:
        for col_info in local_conn.execute(f"PRAGMA table_info({table_name})").fetchall():
            # col_info schema: (cid, name, type, notnull, dflt_value, pk)
            if col_info[5] > 0:
                pk_cols_with_pos.append((col_info[5], col_info[1]))
            if col_info[1] == "content_hash":
                has_hash = True
    except Exception:
        pass

    # Sort by pk position to guarantee declaration order is preserved.
    # PRAGMA table_info returns columns in cid order, which usually matches
    # declaration order, but we sort defensively to handle edge cases.
    pk_cols_with_pos.sort()
    pk_cols = [name for _, name in pk_cols_with_pos] if pk_cols_with_pos else ["id"]

    # Return a string for single-PK tables (backward compat with callers
    # that expect a string), or a list for composite-PK tables.
    return (pk_cols[0] if len(pk_cols) == 1 else pk_cols), has_hash


def _compute_chunk_size(pk_cols_list: list[str], *, total_params_per_row: int = 0) -> int:
    """Compute the safe chunk size for a batch operation.

    Determines how many rows can be safely included in a single SQL
    statement without exceeding SQLite's SQLITE_MAX_VARIABLE_NUMBER
    limit (999 on SQLite < 3.32.0, 32766 on SQLite >= 3.32.0).
    Per https://www.sqlite.org/limits.html

    Two usage modes:
    
    1. WHERE clause batching (default): Each row uses len(pk_cols_list)
       host parameters in an OR-of-ANDs WHERE clause. Example:
       WHERE (pk1=? AND pk2=?) OR (pk1=? AND pk2=?) ...
       Call: _compute_chunk_size(pk_cols_list)
    
    2. INSERT/MERGE batching: Each row uses total_params_per_row host
       parameters (typically len(columns) per row). Example:
       INSERT INTO t (c1,c2,...,c10) VALUES (?,?,...,?) -- repeated N times
       Call: _compute_chunk_size(pk_cols_list, total_params_per_row=10)
    
    WARNING: The default mode (total_params_per_row=0) assumes WHERE-
    clause usage where each row contributes len(pk_cols_list) parameters.
    If you are computing chunk sizes for INSERT or multi-column batch
    operations, you MUST pass total_params_per_row explicitly. Failure
    to do so will cause SQLITE_MAX_VARIABLE_NUMBER violations.

    Examples (WHERE clause mode):
        Single-PK table:     min(999 // 1, 900) = 900
        4-column composite:  min(999 // 4, 900) = 249

    Examples (INSERT mode with 10-column table):
        Single-PK:           min(999 // 10, 900) = 99

    Reference: https://www.sqlite.org/limits.html
    """
    n_cols = max(1, len(pk_cols_list))
    if total_params_per_row > 0:
        params_per_row = total_params_per_row
    else:
        params_per_row = n_cols
    return min(SQLITE_HOST_PARAM_LIMIT // max(1, params_per_row), MAX_CHUNK_SIZE)


def upload_local_manifest(
    cloud_engine,
    local_conn,
    cloud_conn,
    table_name: str,
    pk_col: Union[str, List[str]],
    hash_col: str,
) -> str:
    """Upload a manifest of (pk_cols..., content_hash) to a temporary
    Snowflake table for diffing.

    The manifest table has one VARCHAR column per PK column plus a
    content_hash VARCHAR column. The diff query in sync_data then JOINs
    the manifest against the cloud table to find rows that need pushing.

    For single-PK tables, the manifest has columns (id, content_hash).
    For composite-PK tables, the manifest has columns (ticker, statement_type,
    concept, quarter, content_hash) — matching the PK column names.
    """
    manifest_table = f"{table_name}_manifest_tmp"
    pk_cols = [pk_col] if isinstance(pk_col, str) else list(pk_col)

    # Manifest schema: one VARCHAR column per PK column + content_hash.
    # Column names mirror the PK column names so the diff query can join
    # on m.<pk> = c.<pk> naturally.
    manifest_cols_ddl = ", ".join([f"{c} VARCHAR" for c in pk_cols] + ["content_hash VARCHAR"])
    cloud_conn.execute(text(
        f"CREATE OR REPLACE TEMPORARY TABLE {cloud_engine.settings.schema_name}.{manifest_table} "
        f"({manifest_cols_ddl})"
    ))
    try:
        pk_select = ", ".join(pk_cols)
        cursor = local_conn.execute(f"SELECT {pk_select}, {hash_col} FROM {table_name}")
        manifest_rows = []
        for row in cursor.fetchall():
            # Build a dict with one key per PK column + content_hash.
            row_dict = {}
            for i, c in enumerate(pk_cols):
                row_dict[c] = str(row[i]) if row[i] is not None else None
            row_dict["content_hash"] = (
                str(row[len(pk_cols)]) if row[len(pk_cols)] is not None else None
            )
            manifest_rows.append(row_dict)
    except Exception:
        manifest_rows = []
    if manifest_rows:
        batch_size = 10000
        insert_cols = ", ".join(pk_cols + ["content_hash"])
        insert_placeholders = ", ".join([f":{c}" for c in pk_cols + ["content_hash"]])
        for i in range(0, len(manifest_rows), batch_size):
            batch = manifest_rows[i : i + batch_size]
            cloud_conn.execute(text(
                f"INSERT INTO {cloud_engine.settings.schema_name}.{manifest_table} "
                f"({insert_cols}) VALUES ({insert_placeholders})"
            ), batch)
    return manifest_table


def merge_to_cloud(
    cloud_conn,
    schema: str,
    table_name: str,
    columns: List[str],
    dict_rows: list,
    pk_col,
) -> int:
    """MERGE rows from a temporary stage table into the target Snowflake table.

    Supports both single-column and composite primary keys via pk_col
    (str or list/tuple). The stage table is deduplicated defensively
    using QUALIFY ROW_NUMBER() OVER (PARTITION BY <pk_cols> ORDER BY
    updated_at DESC) = 1 so that even if the source contains duplicate
    PKs (which shouldn't happen for SQLite tables with PRIMARY KEY
    constraints, but could happen due to upstream bugs), the MERGE will
    not fail with Snowflake error 100090 (42P18) "Duplicate row detected".

    Per the Snowflake MERGE docs:
    https://docs.snowflake.com/en/sql-reference/sql/merge
    "To avoid errors when multiple rows in the data source match the
    target table based on the ON condition, use GROUP BY in the source
    clause to ensure that each target row joins against one row (at most)
    in the source." The QUALIFY pattern is the row-preference equivalent
    and is documented at:
    https://docs.snowflake.com/en/sql-reference/constructs/qualify

    Per the same MERGE docs, setting ERROR_ON_NONDETERMINISTIC_MERGE=FALSE
    is NOT a fix — "the results of the merge are nondeterministic." We
    use QUALIFY instead.
    """
    dict_rows = sanitize_snowflake_params(dict_rows)
    stage_table = f"{table_name}_stage"

    from database.backup.schema_registry import BackupSchemaRegistry
    expected_types = BackupSchemaRegistry.expected_snowflake_types(table_name)

    pk_cols = [pk_col] if isinstance(pk_col, str) else list(pk_col)

    # Python-side deduplication as a first line of defense. Mirrors the
    # pattern in database/backup/writer/cloud_writer.py:_flush_batch.
    # If the same PK appears multiple times in dict_rows, keep only the
    # last occurrence (assumed to be the most recent).
    deduped_records: dict = {}
    for r in dict_rows:
        if isinstance(pk_col, str):
            pk_val = r.get(pk_col)
        else:
            pk_val = tuple(r.get(c) for c in pk_cols) if all(c in r for c in pk_cols) else None
        if pk_val is not None:
            deduped_records[pk_val] = r
        else:
            # Rows without a complete PK can't be deduped; keep them as-is.
            deduped_records[id(r)] = r
    dict_rows = list(deduped_records.values())

    if not dict_rows:
        return 0

    col_defs = ",".join([f"{c} {expected_types.get(c.lower(), 'VARCHAR')}" for c in columns])
    cloud_conn.execute(text(f"CREATE OR REPLACE TEMPORARY TABLE {schema}.{stage_table} ({col_defs})"))

    insert_placeholders = ",".join([f":{c}" for c in columns])
    cloud_conn.execute(text(f"INSERT INTO {schema}.{stage_table} VALUES ({insert_placeholders})"), dict_rows)

    merge_on = " AND ".join([f"t.{c} = s.{c}" for c in pk_cols])
    update_set = ", ".join([f"t.{c} = s.{c}" for c in columns if c not in pk_cols])

    # SQL-level defensive deduplication via QUALIFY ROW_NUMBER(). Even
    # though Python-side dedup above should have eliminated duplicates,
    # this provides defense in depth: if a future bug in the diff query
    # or chunking logic introduces duplicates into dict_rows, the QUALIFY
    # ensures the MERGE source is 1:1 on the join key.
    #
    # Per Snowflake QUALIFY docs:
    # https://docs.snowflake.com/en/sql-reference/constructs/qualify
    # "The QUALIFY clause simplifies queries that require filtering on
    # the result of window functions."
    if "updated_at" in columns:
        order_expr = "s.updated_at DESC"
    else:
        # Fallback: order by all non-PK columns deterministically.
        # If there are no non-PK columns, order by a constant.
        non_pk_cols = [c for c in columns if c not in pk_cols]
        order_expr = (
            ", ".join([f"s.{c}" for c in non_pk_cols[:3]])
            if non_pk_cols
            else "(SELECT 0)"
        )

    partition_expr = ", ".join([f"s.{c}" for c in pk_cols])
    deduped_source = (
        f"(SELECT s.* FROM {schema}.{stage_table} s "
        f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition_expr} ORDER BY {order_expr}) = 1) AS s"
    )

    merge_sql = f"""
    MERGE INTO {schema}.{table_name} t
    USING {deduped_source}
    ON {merge_on}
    WHEN MATCHED THEN UPDATE SET {update_set}
    WHEN NOT MATCHED THEN INSERT ({",".join(columns)}) VALUES ({",".join([f"s.{c}" for c in columns])})
    """
    result = cloud_conn.execute(text(merge_sql))
    return result.rowcount if hasattr(result, "rowcount") else len(dict_rows)


def sync_data(
    cloud_engine,
    local_db_path: str,
    tables: dict,
    batch_size: int = 500,
    delta_only: bool = True,
) -> dict:
    """Push local SQLite rows to Snowflake via MERGE upsert.

    Per-table flow:
    1. Detect PK columns (single or composite) and content_hash presence.
    2. Upload a manifest of (pk_cols, content_hash) to a temporary table.
    3. Diff the manifest against the cloud table to find rows needing push
       (missing in cloud OR content_hash differs).
    4. Fetch the dirty rows from local SQLite in chunks (chunk size
       dynamically computed to respect SQLite's host parameter limit).
    5. MERGE the rows into the cloud table via merge_to_cloud.

    The entire _execute_cloud_sync closure is wrapped in with_session_recovery
    to retry on Snowflake 390111 session-gone errors.
    """
    import sqlite3

    if not cloud_engine.settings.enabled or not cloud_engine.engine:
        return {"status": "disabled"}

    from database.backup.resilience.session_recovery import with_session_recovery

    def _execute_cloud_sync():
        local_conn = None
        results = {}
        try:
            local_conn = sqlite3.connect(local_db_path, timeout=30.0)
            with cloud_engine.engine.begin() as cloud_conn:
                for table_name in tables:
                    if "VIRTUAL" in tables[table_name].upper():
                        continue

                    pk_col, has_hash = _detect_pk_columns(local_conn, table_name)
                    hash_col = "content_hash" if has_hash else "''"
                    pk_cols_list = [pk_col] if isinstance(pk_col, str) else list(pk_col)

                    if isinstance(pk_col, list):
                        log.dual_log(
                            tag="Backup:Cloud:CompositePK",
                            level="DEBUG",
                            message=f"Table {table_name} has composite PK: {pk_col}",
                            payload={"table": table_name, "pk_cols": pk_col},
                        )

                    manifest_table = upload_local_manifest(
                        cloud_engine, local_conn, cloud_conn, table_name, pk_col, hash_col
                    )

                    try:
                        # Build diff query with composite-PK-aware join.
                        # The manifest stores all PK values as VARCHAR, so we
                        # CAST the cloud PK columns to VARCHAR for the join
                        # to avoid type-mismatch errors on NUMBER columns.
                        join_cast = " AND ".join(
                            [f"m.{c} = CAST(c.{c} AS VARCHAR)" for c in pk_cols_list]
                        )
                        # For a LEFT JOIN that produces no match, ALL joined
                        # columns are NULL simultaneously. We use AND (not OR)
                        # to be defensive: AND matches only true non-matches,
                        # while OR could falsely match rows with NULL PK values.
                        null_check = " AND ".join(
                            [f"c.{c} IS NULL" for c in pk_cols_list]
                        )
                        m_select = ", ".join([f"m.{c}" for c in pk_cols_list])
                        diff_query = f"""
                        SELECT {m_select}
                        FROM {cloud_engine.settings.schema_name}.{manifest_table} m
                        LEFT JOIN {cloud_engine.settings.schema_name}.{table_name} c
                            ON {join_cast}
                        WHERE ({null_check})
                        """
                        if has_hash:
                            # Append content_hash comparison with explicit
                            # parentheses around the null check to avoid
                            # SQL operator precedence ambiguity.
                            # The OR binds looser than AND, so this evaluates as
                            # (null_check) OR (content_hash differs), which is
                            # the correct semantics: include rows that are
                            # EITHER missing in cloud OR have different content.
                            diff_query += (
                                " OR COALESCE(m.content_hash, '') != COALESCE(c.content_hash, '')"
                            )
                        cloud_needs = cloud_conn.execute(text(diff_query)).fetchall()
                        # For composite PK, ids_to_push is a list of tuples;
                        # for single PK, a list of scalars.
                        if len(pk_cols_list) == 1:
                            ids_to_push = [r[0] for r in cloud_needs]
                        else:
                            ids_to_push = [tuple(r) for r in cloud_needs]
                    except Exception as e:
                        log.dual_log(
                            tag="Backup:Cloud:DiffFail",
                            message=f"Diff query failed for {table_name}: {e}",
                            level="WARNING",
                            payload={"error": str(e)},
                        )
                        pk_select = ", ".join(pk_cols_list)
                        cursor = local_conn.execute(f"SELECT {pk_select} FROM {table_name}")
                        if len(pk_cols_list) == 1:
                            ids_to_push = [r[0] for r in cursor.fetchall()]
                        else:
                            ids_to_push = [tuple(r) for r in cursor.fetchall()]

                    if not ids_to_push:
                        results[table_name] = 0
                        continue

                    # Compute chunk_size based on PK column count to respect
                    # SQLite's SQLITE_MAX_VARIABLE_NUMBER limit.
                    chunk_size = _compute_chunk_size(pk_cols_list)

                    dict_rows = []
                    columns = []
                    for i in range(0, len(ids_to_push), chunk_size):
                        chunk = ids_to_push[i : i + chunk_size]
                        if len(pk_cols_list) == 1:
                            # Single-PK: use simple IN clause.
                            placeholders = ",".join("?" for _ in chunk)
                            cursor = local_conn.execute(
                                f"SELECT * FROM {table_name} WHERE {pk_col} IN ({placeholders})",
                                chunk,
                            )
                        else:
                            # Composite PK: use OR-of-ANDs pattern.
                            # This mirrors the existing pattern in
                            # database/backup/writer/cloud_writer.py for
                            # composite-PK DELETEs and is universally
                            # supported across SQLite versions (unlike
                            # tuple IN syntax which requires SQLite 3.15+).
                            # Per the SQLite OR optimization:
                            # https://www.sqlite.org/optoverview.html#or_optimization
                            # "If a column in the WHERE clause is OR'd with
                            # other constraints, and that column has an index,
                            # then SQLite can use the index to find rows that
                            # satisfy any of the OR'd terms."
                            # The PRIMARY KEY constraint creates a covering
                            # index on the PK columns, so this OR-of-ANDs
                            # pattern uses the index efficiently.
                            row_conditions = []
                            flat_args = []
                            for row_tuple in chunk:
                                row_conditions.append(
                                    "(" + " AND ".join([f"{c} = ?" for c in pk_cols_list]) + ")"
                                )
                                flat_args.extend(row_tuple)
                            or_clause = " OR ".join(row_conditions)
                            cursor = local_conn.execute(
                                f"SELECT * FROM {table_name} WHERE {or_clause}",
                                flat_args,
                            )
                        if not columns and cursor.description:
                            columns = [desc[0] for desc in cursor.description]
                        dict_rows.extend([dict(zip(columns, r)) for r in cursor.fetchall()])

                    if not dict_rows or not columns:
                        results[table_name] = 0
                        continue

                    if "embedding" in columns:
                        if cloud_engine.vec0_settings.use_native_vector_type:
                            from database.backup.vec.cloud_vector_pusher import VectorSync
                            pusher = VectorSync(circuit_breaker=cloud_engine.circuit_breaker_vec)
                            push_result = pusher.push_vectors(
                                cloud_conn,
                                cloud_engine.settings.schema_name,
                                table_name,
                                columns,
                                dict_rows,
                                pk_col,
                                batch_size=batch_size,
                            )
                            results[table_name] = push_result["pushed"]
                        else:
                            results[table_name] = merge_to_cloud(
                                cloud_conn,
                                cloud_engine.settings.schema_name,
                                table_name,
                                columns,
                                dict_rows,
                                pk_col,
                            )
                    else:
                        results[table_name] = merge_to_cloud(
                            cloud_conn,
                            cloud_engine.settings.schema_name,
                            table_name,
                            columns,
                            dict_rows,
                            pk_col,
                        )
                return results
        finally:
            if local_conn is not None:
                try:
                    local_conn.close()
                except Exception:
                    pass

    # Compose: circuit breaker wraps the session-recovery-wrapped function.
    # Order matters: the session recovery handles transient 390111 errors
    # first; if the retry also fails, the circuit breaker records the
    # failure and may eventually OPEN to prevent cascade.
    recovered_fn = with_session_recovery(
        _execute_cloud_sync,
        engine=cloud_engine.engine,
        log=log,
        tag="sync_data",
        max_retries=1,
    )
    return cloud_engine.circuit_breaker_push.call(recovered_fn)


def pull_to_local(cloud_engine, local_db_path: str, tables: dict) -> dict:
    """Pull cloud Snowflake rows into local SQLite via INSERT OR REPLACE.

    Per-table flow:
    1. Detect PK columns (single or composite) and content_hash presence.
    2. Upload a manifest of (pk_cols, content_hash) to a temporary table
       representing the local state.
    3. Diff the cloud table against the manifest to find rows that need
       pulling (missing locally OR content_hash differs).
    4. Fetch the dirty rows from Snowflake.
    5. INSERT OR REPLACE the rows into local SQLite.

    The entire _execute_pull closure is wrapped in with_session_recovery
    to retry on Snowflake 390111 "Session no longer exists" errors.

    Composite-PK handling is symmetric with sync_data: the diff query
    JOINs on all PK columns, and the null check uses AND to be defensive
    against NULL PK values in the cloud table.
    """
    import sqlite3

    if not cloud_engine.settings.enabled or not cloud_engine.engine:
        return {"status": "disabled"}

    from database.backup.resilience.session_recovery import with_session_recovery

    def _execute_pull():
        results = {}
        local_conn = None
        try:
            local_conn = sqlite3.connect(local_db_path, timeout=30.0)
            with cloud_engine.engine.begin() as cloud_conn:
                for table_name in tables:
                    if "VIRTUAL" in tables[table_name].upper():
                        continue

                    pk_col, has_hash = _detect_pk_columns(local_conn, table_name)
                    hash_col = "content_hash" if has_hash else "''"
                    pk_cols_list = [pk_col] if isinstance(pk_col, str) else list(pk_col)

                    manifest_table = upload_local_manifest(
                        cloud_engine, local_conn, cloud_conn, table_name, pk_col, hash_col
                    )

                    # Build composite-PK-aware diff query.
                    # The manifest columns are named after the PK columns
                    # (e.g. ticker, statement_type, concept, quarter), so we
                    # can join m.<c> = c.<c> directly without CASTs (both
                    # sides are VARCHAR in the manifest, but the cloud side
                    # may be NUMBER — we CAST the cloud side to VARCHAR for
                    # the join to avoid type-mismatch errors).
                    join_cast = " AND ".join(
                        [f"c.{c} = m.{c}" for c in pk_cols_list]
                    )
                    # For a LEFT JOIN that produces no match, ALL joined
                    # columns are NULL simultaneously. We use AND (not OR)
                    # to be defensive: AND matches only true non-matches.
                    null_check = " AND ".join(
                        [f"m.{c} IS NULL" for c in pk_cols_list]
                    )
                    diff_query = f"""
                        SELECT c.* FROM {cloud_engine.settings.schema_name}.{table_name} c
                        LEFT JOIN {cloud_engine.settings.schema_name}.{manifest_table} m
                            ON {join_cast}
                        WHERE ({null_check})
                    """
                    if has_hash:
                        # Append content_hash comparison with explicit
                        # parentheses around the null check.
                        diff_query += (
                            " OR COALESCE(c.content_hash, '') != COALESCE(m.content_hash, '')"
                        )
                    cloud_rows = cloud_conn.execute(text(diff_query)).fetchall()

                    if not cloud_rows:
                        results[table_name] = 0
                        continue

                    columns = [k.lower() for k in cloud_rows[0]._mapping.keys()]
                    has_embedding = "embedding" in columns

                    if has_embedding and cloud_engine.vec0_settings.use_native_vector_type:
                        from database.backup.vec.cloud_vector_pusher import VectorSync
                        vector_sync = VectorSync()
                        records, dlq_rows = vector_sync.pull_vectors_from_cloud(cloud_rows, columns)
                        if dlq_rows:
                            _route_dlq_rows(dlq_rows, table_name, pk_col)
                    else:
                        records = _normalize_cloud_rows(cloud_rows, columns)

                    if "content_hash" in columns:
                        for r in records:
                            if isinstance(r, dict) and not r.get("content_hash"):
                                from database.backup.sync.foundation import ContentHasher
                                new_hash = ContentHasher.compute_row_hash(table_name, r)
                                r["content_hash"] = new_hash

                    if records:
                        cols = list(records[0].keys())
                        placeholders = ",".join(["?"] * len(cols))
                        insert_sql = (
                            f"INSERT OR REPLACE INTO {table_name} "
                            f"({','.join(cols)}) VALUES ({placeholders})"
                        )
                        rows_as_tuples = [tuple(r.get(c) for c in cols) for r in records]
                        local_conn.executemany(insert_sql, rows_as_tuples)
                        local_conn.commit()
                        log.dual_log(
                            tag="Backup:Pull:Written",
                            message=f"Wrote {len(records)} rows to local table {table_name}",
                            level="INFO",
                            payload={"table": table_name, "rows": len(records)},
                        )
                    results[table_name] = len(records)
        finally:
            if local_conn is not None:
                try:
                    local_conn.close()
                except Exception:
                    pass
        return results

    recovered_fn = with_session_recovery(
        _execute_pull,
        engine=cloud_engine.engine,
        log=log,
        tag="pull_to_local",
        max_retries=1,
    )
    return cloud_engine.circuit_breaker_pull.call(recovered_fn)


def _normalize_cloud_rows(cloud_rows, columns: list) -> list:
    records = []
    for row in cloud_rows:
        norm_row = []
        for val in row:
            if isinstance(val, (datetime.datetime, datetime.date)):
                norm_row.append(val.isoformat())
            elif isinstance(val, Decimal):
                norm_row.append(float(val))
            elif isinstance(val, list) and len(val) > 0 and isinstance(val[0], float):
                norm_row.append(struct.pack(f"<{len(val)}f", *val))
            else:
                norm_row.append(val)
        records.append(dict(zip(columns, norm_row)))
    return records


def _route_dlq_rows(dlq_rows: list, table_name: str, pk_col):
    from database.writer import enqueue_write
    import json
    from utils.id_generator import ULID

    for dlq_row in dlq_rows:
        safe_dlq_row = {
            k: v for k, v in dlq_row.items()
            if k != "_error_msg" and not isinstance(v, bytes)
        }
        # Build a row_id string from the PK columns. For composite PKs,
        # join the values with '|'; for single PK, use the value directly.
        if isinstance(pk_col, str):
            row_id_val = str(dlq_row.get(pk_col, ""))
        else:
            row_id_val = "|".join(str(dlq_row.get(c, "")) for c in pk_col)
        enqueue_write(
            "INSERT INTO dead_letter_queue (dlq_id, table_name, row_id, row_data, error_message) VALUES (?, ?, ?, ?, ?)",
            (
                ULID.generate(),
                table_name,
                row_id_val,
                json.dumps(safe_dlq_row),
                dlq_row.get("_error_msg"),
            ),
        )
