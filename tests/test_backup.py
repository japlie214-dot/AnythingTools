"""tests/test_backup.py
Unit tests for the backup subsystem.
Run with: python -m pytest tests/test_backup.py -v
"""
import pytest
import pyarrow as pa
import pandas as pd
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.backup.schema import (
    TABLE_SCHEMAS,
    VECTOR_BYTE_LENGTH,
    FLOAT32_COUNT,
    validate_embedding_bytes,
    SCRAPED_ARTICLES_VEC_SCHEMA,
    LONG_TERM_MEMORIES_VEC_SCHEMA,
)

class TestSchemaDefinitions:
    def test_vector_schema_uses_binary(self):
        for schema_name in ["scraped_articles_vec", "long_term_memories_vec"]:
            schema = TABLE_SCHEMAS[schema_name]
            emb_field = schema.field("embedding")
            assert pa.types.is_binary(emb_field.type)

    def test_all_schemas_are_valid_pyarrow(self):
        for name, schema in TABLE_SCHEMAS.items():
            assert isinstance(schema, pa.Schema)
            assert len(schema.names) > 0

    def test_vector_byte_length_matches_float32_count(self):
        assert VECTOR_BYTE_LENGTH == FLOAT32_COUNT * 4

class TestEmbeddingValidation:
    def test_valid_embedding_passes(self):
        valid = b"\x00" * VECTOR_BYTE_LENGTH
        validate_embedding_bytes(valid)

    def test_none_embedding_raises(self):
        with pytest.raises(ValueError):
            validate_embedding_bytes(None)  # type: ignore[arg-type]

    def test_wrong_size_embedding_raises(self):
        wrong = b"\x00" * (VECTOR_BYTE_LENGTH - 4)
        with pytest.raises(ValueError):
            validate_embedding_bytes(wrong)

class TestSchemaPandasRoundtrip:
    def test_vector_table_from_pandas(self):
        df = pd.DataFrame({
            "rowid": [1, 2, 3],
            "embedding": [b"\x00" * VECTOR_BYTE_LENGTH for _ in range(3)],
        })
        table = pa.Table.from_pandas(df, schema=SCRAPED_ARTICLES_VEC_SCHEMA, preserve_index=False)
        assert table.schema.equals(SCRAPED_ARTICLES_VEC_SCHEMA)
        assert table.num_rows == 3

    def test_vector_table_wrong_size_fails_validation(self):
        df = pd.DataFrame({
            "rowid": [1],
            "embedding": [b"\x00" * (VECTOR_BYTE_LENGTH - 1)],
        })
        # Validation helper should catch the wrong-sized embedding before conversion
        with pytest.raises(ValueError):
            validate_embedding_bytes(df.iloc[0]["embedding"])  # type: ignore[index]

class TestWatermarkCompatibility:
    def test_model_dump_compat(self):
        from tools.backup.models import Watermark
        wm = Watermark(last_article_id="test123", total_articles_exported=42)
        d = wm.model_dump_compat()
        assert d["last_article_id"] == "test123"
        assert d["total_articles_exported"] == 42
        assert "table_watermarks" in d
