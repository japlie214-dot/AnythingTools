# tests/test_backup.py
import pytest
from pathlib import Path
import sys
import struct

sys.path.insert(0, str(Path(__file__).parent.parent))

# ─── Constants ─────────────────────────────────────────────────────────
VECTOR_DIM = 1024
VECTOR_BYTES = VECTOR_DIM * 4

def make_valid_embedding_blob(seed: float = 0.1) -> bytes:
    return struct.pack(f'<{VECTOR_DIM}f', *[seed + i * 0.001 for i in range(VECTOR_DIM)])

def make_valid_float_list(seed: float = 0.1) -> list:
    return [seed + i * 0.001 for i in range(VECTOR_DIM)]

class TestVectorSyncValidation:
    def test_validate_valid_vector(self):
        from database.backup.vec.cloud_vector_pusher import VectorSync
        VectorSync.validate_vector(make_valid_float_list())

    def test_validate_wrong_dimensions(self):
        from database.backup.vec.cloud_vector_pusher import VectorSync, VectorValidationError
        with pytest.raises(VectorValidationError):
            VectorSync.validate_vector([0.1] * 512)

    def test_validate_nan_in_vector(self):
        from database.backup.vec.cloud_vector_pusher import VectorSync, VectorValidationError
        values = make_valid_float_list()
        values[0] = float('nan')
        with pytest.raises(VectorValidationError):
            VectorSync.validate_vector(values)

class TestBlobConversion:
    def test_blob_to_float_list_roundtrip(self):
        from database.backup.vec.cloud_vector_pusher import VectorSync
        original = make_valid_float_list()
        blob = VectorSync.float_list_to_blob(original)
        recovered = VectorSync.blob_to_float_list(blob)
        assert len(recovered) == VECTOR_DIM

class TestValidateAndNormalize:
    def test_normalize_wrong_dimensions_routes_to_dlq(self):
        from database.backup.vec.cloud_vector_pusher import VectorSync
        sync = VectorSync()
        rows = [{"id": "1", "embedding": [0.1] * 512}]
        valid, dlq = sync._validate_and_normalize(rows)
        assert len(valid) == 0
        assert len(dlq) == 1
        assert isinstance(dlq[0]["embedding"], list)

class TestSchemaDefinitions:
    def test_schema_registry_snowflake_mappings(self):
        from database.backup.schema_registry import BackupSchemaRegistry
        vec0_ddl = BackupSchemaRegistry.get_snowflake_ddl("scraped_articles_vec")
        backup_ddl = BackupSchemaRegistry.get_snowflake_ddl("scraped_articles_vec_backup")
        assert "VECTOR(FLOAT, 1024)" in vec0_ddl
        assert "VECTOR(FLOAT, 1024)" in backup_ddl

class TestEmbeddingValidation:
    def test_valid_embedding_passes(self):
        from database.backup.vec.adapter import _validate_embedding_blob
        valid = b"\x00" * VECTOR_BYTES
        _validate_embedding_blob(valid, 1)

    def test_none_embedding_passes(self):
        from database.backup.vec.adapter import _validate_embedding_blob
        # None is bypassed silently in adapter
        _validate_embedding_blob(None, 1)

    def test_wrong_size_embedding_raises(self):
        from database.backup.vec.adapter import _validate_embedding_blob
        wrong = b"\x00" * (VECTOR_BYTES - 4)
        with pytest.raises(ValueError):
            _validate_embedding_blob(wrong, 1)

class TestWatermarkCompatibility:
    def test_model_dump_compat(self):
        from database.backup.models import Watermark
        wm = Watermark(last_article_id="test123", total_articles_exported=42)
        d = wm.model_dump_compat()
        assert d["last_article_id"] == "test123"
        assert d["total_articles_exported"] == 42
        assert "table_watermarks" in d
