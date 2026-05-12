# clients/snowflake_client.py
import json
import threading
from typing import Any

class EmbeddingError(Exception):
    """Base class for embedding pipeline failures."""

class EmbeddingTypeError(EmbeddingError):
    """Raised when Snowflake returns an unparseable VECTOR type."""

class EmbeddingDimensionError(EmbeddingError):
    """Raised when the extracted vector has wrong dimension count."""

class EmbeddingValidationError(EmbeddingError):
    """Raised when the vector contains NaN, Inf, or non-finite values."""
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

import config
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class SnowflakeClient:
    _instance = None
    _lock = threading.RLock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.conn = None
        return cls._instance

    def verify_or_reconnect(self) -> None:
        """Probe session vitality and silently re-authenticate if the session has dropped.
        The lock is held for the entire check-and-replace cycle to prevent concurrent
        re-authentication races and cursor leaks.
        """
        with self._lock:
            if self.conn is not None:
                try:
                    cur = self.conn.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                    return  # Session is alive; nothing to do.
                except Exception:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = None  # Nullify before re-auth to avoid stale-cursor leaks.

            if not config.SNOWFLAKE_ACCOUNT or not config.SNOWFLAKE_USER:
                raise RuntimeError(
                    "Snowflake credentials not configured. "
                    "Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER environment variables."
                )
            key_path = Path(config.SNOWFLAKE_PRIVATE_KEY_PATH)
            if not key_path.exists():
                raise FileNotFoundError(
                    f"Snowflake private key not found at: {key_path}"
                )
            with key_path.open("rb") as kf:
                p_key = serialization.load_pem_private_key(
                    kf.read(), password=None, backend=default_backend()
                )
            pkb = p_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            self.conn = snowflake.connector.connect(
                account=config.SNOWFLAKE_ACCOUNT,
                user=config.SNOWFLAKE_USER,
                private_key=pkb,
                database=config.SNOWFLAKE_DATABASE,
                schema=config.SNOWFLAKE_SCHEMA,
                warehouse=config.SNOWFLAKE_WAREHOUSE,
                client_session_keep_alive=True,
            )
            log.dual_log(
                tag="Snowflake:Client:Init",
                message="Established new Snowflake connection via .p8 key-pair.",
                payload={"account_last4": config.SNOWFLAKE_ACCOUNT[-4:] if config.SNOWFLAKE_ACCOUNT else None, "user": config.SNOWFLAKE_USER, "key_path": str(key_path)},
            )

    @staticmethod
    def _extract_vector(raw: Any) -> list[float]:
        if isinstance(raw, list):
            return raw[0] if raw and isinstance(raw[0], list) else raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception as exc:
                raise EmbeddingTypeError(f"Snowflake returned str that is not valid JSON: {exc}") from exc
            return SnowflakeClient._extract_vector(parsed)
        if isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, list) and v and isinstance(v[0], (int, float)):
                    return v
            raise EmbeddingTypeError(f"Snowflake returned dict with no list-of-floats value. Keys: {list(raw.keys())}")
        try:
            return list(raw)
        except Exception as exc:
            raise EmbeddingTypeError(f"Cannot convert Snowflake result to list: {exc}") from exc

    def embed(self, text: str, model: str = "voyage-multilingual-2") -> list[float]:
        """Generate a 1024-dimensional vector embedding using Snowflake Cortex AI_EMBED.

        Synchronous — must be called via asyncio.to_thread() in async contexts,
        or via asyncio.run_coroutine_threadsafe() from the botasaurus browser thread.
        """
        safe_text = text[:8000]
        log.dual_log(
            tag="Snowflake:Embed:Request",
            message=f"Sending embedding request ({len(safe_text)} chars)",
            payload={"text_length": len(safe_text), "text_preview": safe_text[:500], "model": model}
        )
        
        with self._lock:
            self.verify_or_reconnect()
            cursor = self.conn.cursor()
            try:
                sql = "SELECT AI_EMBED(%s, %s)"
                cursor.execute(sql, (model, safe_text))
                result = cursor.fetchone()
                
                if not result or result[0] is None:
                    raise EmbeddingTypeError("Snowflake AI_EMBED returned empty result.")
                    
                raw_res = result[0]
                raw_type = type(raw_res).__name__
                vec = self._extract_vector(raw_res)
                
                log.dual_log(
                    tag="Embed:Snowflake:Raw",
                    message=f"Extracted vector from Snowflake ({raw_type} → list[{len(vec)}])",
                    payload={"raw_type": raw_type, "dimensions": len(vec)},
                )
            finally:
                cursor.close()
                
        if not isinstance(vec, list) or len(vec) == 0:
            raise EmbeddingTypeError(f"Extracted value is not a non-empty list: {type(vec).__name__}")
            
        try:
            vec = [float(x) for x in vec]
        except Exception as exc:
            raise EmbeddingTypeError(f"Cannot coerce vector elements to float: {exc}") from exc
            
        if len(vec) != 1024:
            raise EmbeddingDimensionError(f"Expected 1024 dimensions, got {len(vec)}")
            
        import math
        if any(math.isnan(x) or math.isinf(x) for x in vec):
            bad_indices = [i for i, x in enumerate(vec) if math.isnan(x) or math.isinf(x)]
            raise EmbeddingValidationError(f"Embedding contains NaN/Inf at indices: {bad_indices[:10]}")
            
        return vec

    async def async_embed(self, text: str, model: str = "voyage-multilingual-2") -> list[float]:
        """
        Asynchronous wrapper for embedding generation to avoid blocking the event loop.
        """
        import asyncio
        return await asyncio.to_thread(self.embed, text, model)


# Module-level singleton — import and use this instance everywhere.
snowflake_client = SnowflakeClient()
