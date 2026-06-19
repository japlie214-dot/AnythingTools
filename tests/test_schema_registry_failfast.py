# tests/test_schema_registry_failfast.py
"""Verify BackupSchemaRegistry.get_snowflake_ddl fails fast on sqlglot errors
instead of returning partially-patched DDL.

Regression test for Violation D: the previous `except Exception:` fallback
returned DDL patched via fragile regex (the exact pattern the AST rewrite
was introduced to replace). The fix surfaces the failure as a RuntimeError
with full diagnostic context.

Ref: sqlglot error hierarchy — https://sqlglot.com/sqlglot/errors.html
"""
import pytest
from unittest.mock import patch
import sqlglot.errors


class TestGetSnowflakeDDLFailFast:
    def test_sqlglot_parse_error_raises_runtime_error(self):
        """If sqlglot.parse_one raises ParseError, get_snowflake_ddl must
        raise RuntimeError (NOT return regex-patched DDL).

        Per https://sqlglot.com/sqlglot/errors.html:
        "When the parser detects an error in the syntax, it raises a ParseError"
        """
        from database.backup.schema_registry import BackupSchemaRegistry

        with patch(
            "database.backup.schema_registry.sqlglot.parse_one",
            side_effect=sqlglot.errors.ParseError("simulated parse failure"),
        ):
            with pytest.raises(RuntimeError, match="Snowflake DDL generation failed"):
                BackupSchemaRegistry.get_snowflake_ddl("scraped_articles")

    def test_sqlglot_unsupported_error_raises_runtime_error(self):
        """UnsupportedError (best-effort mode off) must also fail-fast."""
        from database.backup.schema_registry import BackupSchemaRegistry

        with patch(
            "database.backup.schema_registry.sqlglot.parse_one",
            side_effect=sqlglot.errors.UnsupportedError("simulated unsupported"),
        ):
            with pytest.raises(RuntimeError, match="Snowflake DDL generation failed"):
                BackupSchemaRegistry.get_snowflake_ddl("scraped_articles")

    def test_unexpected_exception_also_raises(self):
        """Non-sqlglot exceptions (programming errors) must also fail-fast."""
        from database.backup.schema_registry import BackupSchemaRegistry

        with patch(
            "database.backup.schema_registry.sqlglot.parse_one",
            side_effect=KeyError("simulated AST bug"),
        ):
            with pytest.raises(RuntimeError, match="Unexpected error generating Snowflake DDL"):
                BackupSchemaRegistry.get_snowflake_ddl("scraped_articles")

    def test_current_timestamp_stripped_from_all_tables(self):
        """The CURRENT_TIMESTAMP default must be stripped from all DDL
        (Snowflake rejects it on VARCHAR columns).

        Per the in-code comment in schema_registry.py:
        "Snowflake rejects DEFAULT CURRENT_TIMESTAMP on VARCHAR columns
         (type mismatch)."
        """
        from database.backup.schema_registry import BackupSchemaRegistry

        for table_name in BackupSchemaRegistry.get_expected_sqlite_tables():
            ddl = BackupSchemaRegistry.get_snowflake_ddl(table_name)
            assert "CURRENT_TIMESTAMP" not in ddl.upper(), (
                f"{table_name}: DDL still contains CURRENT_TIMESTAMP. "
                f"DDL: {ddl[:500]}"
            )
