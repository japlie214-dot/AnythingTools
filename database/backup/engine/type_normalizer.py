# database/backup/engine/type_normalizer.py
"""Normalize Snowflake type strings for base-type comparison.

Snowflake's INFORMATION_SCHEMA.COLUMNS.DATA_TYPE returns only the base
type name for parameterized types (VECTOR, ARRAY, OBJECT, MAP, NUMBER,
VARCHAR, etc.). For example, a column declared as VECTOR(FLOAT, 1024)
returns DATA_TYPE = "VECTOR" (without the parameters).

This module provides functions to normalize both sides of a type
comparison to the base type name, so that override values (which may
include parameters like "VECTOR(FLOAT, 1024)") can be compared against
INFORMATION_SCHEMA output (which is base-type-only) without false
mismatches.

For parameter-level verification (e.g. distinguishing VECTOR(FLOAT, 512)
from VECTOR(FLOAT, 1024)), use SHOW COLUMNS instead — its data_type
column is a JSON object with full parameterization. See:
https://docs.snowflake.com/en/sql-reference/sql/show-columns

References:
    - Snowflake structured data types (documents DATA_TYPE base-type-only
      behavior for ARRAY/OBJECT/MAP; VECTOR follows the same pattern):
      https://docs.snowflake.com/en/sql-reference/data-types-structured
    - Snowflake VECTOR data type:
      https://docs.snowflake.com/en/sql-reference/data-types-vector
    - Snowflake INFORMATION_SCHEMA.COLUMNS view:
      https://docs.snowflake.com/en/sql-reference/info-schema/columns
"""
from __future__ import annotations

import re

# Matches a trailing parenthesized parameter list, e.g. "(FLOAT, 1024)"
# in "VECTOR(FLOAT, 1024)" or "(38,0)" in "NUMBER(38,0)".
# The regex is greedy and handles nested parens by matching everything
# until the closing paren at end-of-string. DOTALL allows newlines
# (defensive — Snowflake types don't normally span lines, but the
# transpiled DDL from sqlglot might).
_PARAM_SUFFIX = re.compile(r"\s*\(.*\)\s*$", re.DOTALL)


def normalize_snowflake_type(type_str: str) -> str:
    """Return the base type name (uppercase, no parameters) for comparison.

    Examples:
        >>> normalize_snowflake_type("VECTOR(FLOAT, 1024)")
        'VECTOR'
        >>> normalize_snowflake_type("NUMBER(38,0)")
        'NUMBER'
        >>> normalize_snowflake_type("VARCHAR(16777216)")
        'VARCHAR'
        >>> normalize_snowflake_type("boolean")
        'BOOLEAN'
        >>> normalize_snowflake_type("")
        ''

    Args:
        type_str: A Snowflake type string, possibly with parenthesized
            parameters. May be empty.

    Returns:
        The uppercase base type name with no parenthesized parameters.
        Returns empty string if input is empty.
    """
    if not type_str:
        return ""
    return _PARAM_SUFFIX.sub("", type_str.strip()).upper()


def types_match(expected: str, actual: str) -> bool:
    """Compare two Snowflake type strings by base type only.

    This is the comparison function used by
    SnowflakeSchemaManager.reconcile_types() to decide whether a column
    needs to be rebuilt. It intentionally ignores parameter differences
    (e.g. VECTOR(FLOAT, 1024) vs VECTOR(FLOAT, 512)) because:

    1. The SNOWFLAKE_COLUMN_OVERRIDES registry (in
       database/schemas/_snowflake_overrides.py) is the source of truth
       for the desired type, including parameters.
    2. The DDL generator (BackupSchemaRegistry.get_snowflake_ddl) always
       produces the full parameterized type from the override.
    3. If the base type matches, the parameters were correct by
       construction (the table was created by our own DDL generator).

    For cases where parameter-level drift is suspected (e.g. a manual
    ALTER TABLE on the Snowflake side changed VECTOR dimensions), use
    SHOW COLUMNS for a full comparison. See:
    https://docs.snowflake.com/en/sql-reference/sql/show-columns

    Args:
        expected: The expected type string (may include parameters).
        actual: The actual type string from INFORMATION_SCHEMA
            (base-type-only for parameterized types).

    Returns:
        True if both normalize to the same base type name.
    """
    return normalize_snowflake_type(expected) == normalize_snowflake_type(actual)
