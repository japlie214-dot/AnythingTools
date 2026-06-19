# database/backup/schema_registry.py
import re
import sqlglot
from sqlglot import exp
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
            from database.backup.settings import Vec0BackupSettings
            dim = Vec0BackupSettings().dim
            return f"CREATE TABLE IF NOT EXISTS {table_name} (rowid NUMBER, embedding VECTOR(FLOAT, {dim}));"

        # Intercept vec_backup table which has BLOB embedding → use VECTOR
        if table_name == "scraped_articles_vec_backup":
            from database.backup.settings import Vec0BackupSettings
            dim = Vec0BackupSettings().dim
            return (
                f"CREATE TABLE IF NOT EXISTS {table_name} ("
                f"rowid NUMBER, "
                f"article_id VARCHAR, "
                f"embedding VECTOR(FLOAT, {dim}), "
                f"hashed_at VARCHAR"
                f");"
            )

        # Transpile standard tables
        sf_ddl = sqlglot.transpile(sqlite_ddl, read='sqlite', write='snowflake')[0]
        
        # Apply structural DDL modifications via sqlglot AST.
        # Snowflake strictly enforces data type matching for DEFAULT constraints.
        # sqlglot maps SQLite TEXT to Snowflake VARCHAR, but CURRENT_TIMESTAMP() returns a TIMESTAMP.
        # We strip the default timestamp constraint for the cloud backup since the local SQLite DB
        # has already generated and populated the timestamp strings.
        
        try:
            from database.schemas._snowflake_overrides import SNOWFLAKE_COLUMN_OVERRIDES
            overrides = SNOWFLAKE_COLUMN_OVERRIDES.get(table_name, {})
            
            # Parse the transpiled DDL into a sqlglot AST for structural modification.
            # Per sqlglot docs: https://github.com/tobymao/sqlglot
            # parse_one() returns a root Expression node; find_all() and set()
            # enable precise modifications immune to substring collision and
            # quoting bugs inherent in regex-based DDL patching.
            tree = sqlglot.parse_one(sf_ddl, dialect="snowflake")
            
            for col_def in tree.find_all(exp.ColumnDef):
                col_name = col_def.name.lower()
                constraints = list(col_def.args.get("constraints") or [])
                new_constraints = []
                
                has_boolean_override = (
                    col_name in overrides
                    and overrides[col_name].upper().startswith("BOOLEAN")
                )
                
                for constraint in constraints:
                    kind = constraint.args.get("kind")
                    
                    # Remove CURRENT_TIMESTAMP defaults.
                    # Snowflake rejects DEFAULT CURRENT_TIMESTAMP on VARCHAR
                    # columns (type mismatch). Since the local SQLite DB has
                    # already populated timestamp values, we can safely strip
                    # the default.
                    if isinstance(kind, exp.DefaultColumnConstraint):
                        val = kind.this
                        # exp.CurrentTimestamp represents CURRENT_TIMESTAMP keyword.
                        # Some sqlglot versions may produce exp.Anonymous for
                        # CURRENT_TIMESTAMP() — handle both.
                        if isinstance(val, exp.CurrentTimestamp):
                            continue  # Remove this constraint
                        if isinstance(val, exp.Anonymous) and "current_timestamp" in val.name.lower():
                            continue  # Remove this constraint
                        
                        # Fix BOOLEAN DEFAULT 0/1 → DEFAULT FALSE/TRUE.
                        # Per Snowflake BOOLEAN docs:
                        # https://docs.snowflake.com/en/sql-reference/data-types-logical
                        # BOOLEAN columns should use DEFAULT FALSE/TRUE.
                        # Some account configurations reject DEFAULT 0/1 with
                        # error 002262 "Default value data type does not match".
                        # sqlglot exp.Boolean renders as TRUE/FALSE keywords.
                        if has_boolean_override:
                            if isinstance(val, exp.Literal) and not val.is_string:
                                if val.this == "0":
                                    constraint = exp.ColumnConstraint(
                                        kind=exp.DefaultColumnConstraint(
                                            this=exp.Boolean(this=False)
                                        )
                                    )
                                elif val.this == "1":
                                    constraint = exp.ColumnConstraint(
                                        kind=exp.DefaultColumnConstraint(
                                            this=exp.Boolean(this=True)
                                        )
                                    )
                    
                    new_constraints.append(constraint)
                
                # Apply type override if this column has one.
                if col_name in overrides:
                    override_type_str = overrides[col_name]
                    try:
                        # Parse the override type by embedding it in a dummy
                        # CREATE TABLE. This lets sqlglot handle the type
                        # string natively, including parameterized types like
                        # VECTOR(FLOAT, 1024).
                        dummy = sqlglot.parse_one(
                            f"CREATE TABLE _ (_c {override_type_str})",
                            dialect="snowflake",
                        )
                        dummy_col = dummy.find(exp.ColumnDef)
                        if dummy_col and dummy_col.args.get("kind"):
                            col_def.set("kind", dummy_col.args["kind"])
                    except Exception:
                        # If sqlglot cannot parse the override type (unlikely
                        # for standard Snowflake types), leave the transpiled
                        # type unchanged. reconcile_types() will detect the
                        # mismatch on next startup and rebuild the table.
                        pass
                
                col_def.set("constraints", new_constraints if new_constraints else None)
            
            sf_ddl = tree.sql(dialect="snowflake")
            
        except ImportError:
            # _snowflake_overrides.py not available — no overrides to apply.
            # Still need to strip CURRENT_TIMESTAMP. Use regex as this is
            # a simple, well-defined token removal that has zero substring
            # collision risk (it matches a SQL keyword, not a column name).
            sf_ddl = re.sub(r"(?i)\s*DEFAULT\s+CURRENT_TIMESTAMP(?:\(\))?", "", sf_ddl)
        except Exception:
            # AST transformation failed for an unexpected reason. Fall back
            # to the raw transpiled DDL with only CURRENT_TIMESTAMP stripped.
            # This is safe because reconcile_types() will detect type
            # mismatches on next startup.
            sf_ddl = re.sub(r"(?i)\s*DEFAULT\s+CURRENT_TIMESTAMP(?:\(\))?", "", sf_ddl)
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
            from database.schemas._snowflake_overrides import SNOWFLAKE_COLUMN_OVERRIDES
            overrides = SNOWFLAKE_COLUMN_OVERRIDES.get(table_name, {})
        except ImportError:
            overrides = {}
        return {c.name.lower(): overrides.get(c.name.lower(), sqlite_type_to_snowflake(c.type)) for c in cols}
