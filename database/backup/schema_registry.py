# database/backup/schema_registry.py
import re
import sqlglot
from typing import Dict, List, Optional
from database.schemas import ALL_TABLES, ALL_VEC_TABLES, PERSISTED_TABLES
from database.connection import SQLITE_VEC_AVAILABLE
from database.management.schema_introspector import _columns_from_ddl_in_memory

class BackupSchemaRegistry:
    @classmethod
    def get_expected_sqlite_tables(cls) -> Dict[str, str]:
        """Returns target master and vector tables strictly ordered by MASTER_TABLES for FK safety."""
        # Force dictionary sorting according to the parent-before-child rules of MASTER_TABLES
        tables = {}
        for t_name in PERSISTED_TABLES:
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

        # Intercept vec_backup table which has BLOB embedding → use VECTOR
        if table_name == "scraped_articles_vec_backup":
            return (
                f"CREATE TABLE IF NOT EXISTS {table_name} ("
                f"rowid NUMBER, "
                f"article_id VARCHAR, "
                f"embedding VECTOR(FLOAT, 1024), "
                f"hashed_at VARCHAR"
                f");"
            )

        # Transpile standard tables
        sf_ddl = sqlglot.transpile(sqlite_ddl, read='sqlite', write='snowflake')[0]
        
        # Snowflake strictly enforces data type matching for DEFAULT constraints.
        # sqlglot maps SQLite TEXT to Snowflake VARCHAR, but CURRENT_TIMESTAMP() returns a TIMESTAMP.
        # We strip the default timestamp constraint for the cloud backup since the local SQLite DB
        # has already generated and populated the timestamp strings.
        sf_ddl = re.sub(r"(?i)\s*DEFAULT\s+CURRENT_TIMESTAMP(?:\(\))?", "", sf_ddl)
        
        try:
            from database.schemas.stock_financials import SNOWFLAKE_COLUMN_OVERRIDES
            overrides = SNOWFLAKE_COLUMN_OVERRIDES.get(table_name, {})
            for col_lower, sf_type in overrides.items():
                pattern = re.compile(
                    rf'(?i)((?:\b|"){re.escape(col_lower)}(?:\b|")\s+)([A-Z0-9_]+(?:\s*\([^)]*\))?)',
                    re.IGNORECASE
                )
                sf_ddl = pattern.sub(rf"\1{sf_type}", sf_ddl)

                # If the override changed the column type to BOOLEAN, convert
                # any numeric DEFAULT (0/1) to BOOLEAN (FALSE/TRUE). Snowflake
                # rejects "BOOLEAN ... DEFAULT 0" in some account configurations
                # with error 002262 "Default value data type does not match
                # data type for column". Per the Snowflake BOOLEAN docs, the
                # correct DEFAULT for a BOOLEAN column is FALSE or TRUE.
                # Ref: https://docs.snowflake.com/en/sql-reference/data-types-logical
                # Ref: https://docs.snowflake.com/en/release-notes/bcr-bundles/2023_08/bcr-1425
                # (BCR-1425 restricts incompatible DEFAULT pairs; BOOLEAN+NUMBER
                # is not explicitly listed but empirically rejected in some
                # accounts, so we convert defensively.)
                if sf_type.upper() == "BOOLEAN":
                    # Convert: <col> BOOLEAN [NOT NULL] DEFAULT 0 → ... DEFAULT FALSE
                    sf_ddl = re.sub(
                        rf'(?i)\b{re.escape(col_lower)}\b\s+BOOLEAN\s+((?:NOT\s+NULL\s+)?DEFAULT\s+)0\b',
                        rf'{col_lower} BOOLEAN \1FALSE',
                        sf_ddl,
                    )
                    # Convert: <col> BOOLEAN [NOT NULL] DEFAULT 1 → ... DEFAULT TRUE
                    sf_ddl = re.sub(
                        rf'(?i)\b{re.escape(col_lower)}\b\s+BOOLEAN\s+((?:NOT\s+NULL\s+)?DEFAULT\s+)1\b',
                        rf'{col_lower} BOOLEAN \1TRUE',
                        sf_ddl,
                    )
        except ImportError:
            pass

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

    @classmethod
    def get_non_nullable_columns(cls, table_name: str) -> List[str]:
        """Returns columns that are NOT NULL in the SQLite schema."""
        sqlite_ddl = cls.get_expected_sqlite_tables().get(table_name)
        if not sqlite_ddl:
            return []
        cols = _columns_from_ddl_in_memory(sqlite_ddl, table_name)
        if not cols:
            return []
        return [c.name for c in cols if c.notnull and c.name.lower() != 'rowid']

    @classmethod
    def expected_snowflake_types(cls, table_name: str) -> Dict[str, str]:
        """Return expected Snowflake column types for a table based on SQLite DDL."""
        from database.management.schema_introspector import _columns_from_ddl_in_memory, sqlite_type_to_snowflake
        sqlite_ddl = cls.get_expected_sqlite_tables().get(table_name)
        if not sqlite_ddl:
            return {}
        cols = _columns_from_ddl_in_memory(sqlite_ddl, table_name)
        if not cols:
            return {}
        try:
            from database.schemas.stock_financials import SNOWFLAKE_COLUMN_OVERRIDES
            overrides = SNOWFLAKE_COLUMN_OVERRIDES.get(table_name, {})
        except ImportError:
            overrides = {}
        return {c.name.lower(): overrides.get(c.name.lower(), sqlite_type_to_snowflake(c.type)) for c in cols}
