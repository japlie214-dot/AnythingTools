# database/backup/schema_registry.py
import re
import sqlglot
from typing import Dict, List
from database.schemas import ALL_TABLES, ALL_VEC_TABLES, MASTER_TABLES
from database.management.schema_introspector import _columns_from_ddl_in_memory

class BackupSchemaRegistry:
    @classmethod
    def get_expected_sqlite_tables(cls) -> Dict[str, str]:
        """Returns only target master tables and vector tables."""
        tables = {k: v for k, v in ALL_TABLES.items() if k in MASTER_TABLES}
        tables.update(ALL_VEC_TABLES)
        return tables

    @classmethod
    def get_snowflake_ddl(cls, table_name: str) -> str:
        sqlite_ddl = cls.get_expected_sqlite_tables().get(table_name)
        if not sqlite_ddl:
            raise ValueError(f"Table {table_name} not found in registry.")

        # Intercept SQLite VIRTUAL TABLE vec0 syntax and map to standard table
        if "VIRTUAL TABLE" in sqlite_ddl.upper() and "vec0" in sqlite_ddl.lower():
            return f"CREATE TABLE IF NOT EXISTS {table_name} (rowid NUMBER, embedding VECTOR(FLOAT, 1024));"

        # Transpile standard tables
        sf_ddl = sqlglot.transpile(sqlite_ddl, read='sqlite', write='snowflake')[0]
        return sf_ddl

    @classmethod
    def get_checksum_columns(cls, table_name: str) -> List[str]:
        """Returns columns for checksums, explicitly excluding embeddings to avoid precision drift."""
        sqlite_ddl = cls.get_expected_sqlite_tables().get(table_name)
        if not sqlite_ddl:
            return []
        cols = _columns_from_ddl_in_memory(sqlite_ddl, table_name)
        if not cols:
            return []
        return [c.name for c in cols if c.name.lower() not in ('embedding', 'vec_rowid')]
