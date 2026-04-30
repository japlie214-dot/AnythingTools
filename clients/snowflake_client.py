# clients/snowflake_client.py
import json
import threading
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

import config
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class SnowflakeClient:
    _instance = None
    _lock = threading.Lock()

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
                tag="Snowflake",
                message="Established new Snowflake connection via .p8 key-pair.",
                payload={"account_last4": config.SNOWFLAKE_ACCOUNT[-4:] if config.SNOWFLAKE_ACCOUNT else None, "user": config.SNOWFLAKE_USER, "key_path": str(key_path)},
            )

    def embed(self, text: str, model: str = "voyage-multilingual-2") -> list[float]:
        """Generate a 1024-dimensional vector embedding using Snowflake Cortex AI_EMBED.

        Synchronous — must be called via asyncio.to_thread() in async contexts,
        or via asyncio.run_coroutine_threadsafe() from the botasaurus browser thread.
        """
        self.verify_or_reconnect()
        # Truncate only; do NOT manually escape quotes. The text is passed as a
        # parameterized bind variable (%s), so the connector handles escaping.
        # Manual replace("'", "''") before parameterization causes double-encoding.
        safe_text = text[:8000]
        cursor = self.conn.cursor()
        try:
            sql = f"SELECT AI_EMBED('{model}', %s)"
            cursor.execute(sql, (safe_text,))
            result = cursor.fetchone()
            if result and result[0] is not None:
                return json.loads(result[0]) if isinstance(result[0], str) else list(result[0])
            raise ValueError("Snowflake AI_EMBED returned an empty result.")
        finally:
            cursor.close()

    async def async_embed(self, text: str, model: str = "voyage-multilingual-2") -> list[float]:
        """
        Asynchronous wrapper for embedding generation to avoid blocking the event loop.
        """
        import asyncio
        return await asyncio.to_thread(self.embed, text, model)


# Module-level singleton — import and use this instance everywhere.
snowflake_client = SnowflakeClient()
