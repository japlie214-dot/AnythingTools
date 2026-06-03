# database/backup/schema_registry.py
import re
import sqlglot
from typing import Dict, List
from database.schemas import ALL_TABLES, ALL_VEC_TABLES, MASTER_TABLES
from database.connection import SQLITE_VEC_AVAILABLE
from database.management.schema_introspector import _columns_from_ddl_in_memory

class BackupSchemaRegistry:
    @classmethod
    def get_expected_sqlite_tables(cls) -> Dict[str, str]:
        """Returns target master and vector tables strictly ordered by MASTER_TABLES for FK safety."""
        # Force dictionary sorting according to the parent-before-child rules of MASTER_TABLES
        tables = {}
        for t_name in MASTER_TABLES:
            if t_name in ALL_TABLES:
                tables[t_name] = ALL_TABLES[t_name]
                
        for name, ddl in ALL_VEC_TABLES.items():
            if SQLITE_VEC_AVAILABLE:
                tables[name] = ddl
            else:
                tables[name] = f"CREATE TABLE IF NOT EXISTS {name} (rowid INTEGER PRIMARY KEY AUTOINCREMENT, embedding BLOB);"
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
        
        # Snowflake strictly enforces data type matching for DEFAULT constraints.
        # sqlglot maps SQLite TEXT to Snowflake VARCHAR, but CURRENT_TIMESTAMP() returns a TIMESTAMP.
        # We strip the default timestamp constraint for the cloud backup since the local SQLite DB
        # has already generated and populated the timestamp strings.
        sf_ddl = re.sub(r"(?i)\s*DEFAULT\s+CURRENT_TIMESTAMP(?:\(\))?", "", sf_ddl)
        
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
