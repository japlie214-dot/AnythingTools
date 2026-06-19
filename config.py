# config.py — AnythingTools configuration (safe defaults)
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- API Security ---
API_KEY: str = os.getenv("API_KEY", "dev_default_key_change_me_in_production")
ANYTHINGTOOLS_PORT: int = int(os.getenv("ANYTHINGTOOLS_PORT", "8000"))

# --- Operational Database Path ---
OPERATIONAL_DB_PATH: str = os.getenv("OPERATIONAL_DB_PATH", "data/sumanal.db")

# --- Database Integration Master Toggle ---
# When False, ALL database writes are skipped:
#   - SQLite operational DB (enqueue_write, enqueue_transaction, enqueue_execscript)
#   - SQLite logs DB (logs_enqueue_write)
#   - Snowflake cloud DB (enqueue_cloud_write, enqueue_cloud_write_batch, enqueue_cloud_delete)
#   - Startup phases: init_database_layer, run_db_migrations, validate_vec0,
#     init_backup, sync_from_backup all become no-ops.
#
# This is DISTINCT from BACKUP_CLOUD_ENABLED (in database/backup/settings.py):
#   - BACKUP_CLOUD_ENABLED=false → cloud sync disabled, SQLite still writes.
#   - DATABASE_INTEGRATION_ENABLED=false → entire DB stack disabled (for testing).
#
# Intended use case: integration tests and local development where you want
# to run the application without a real database (e.g. with mock data).
#
# Per Python os.getenv docs: https://docs.python.org/3/library/os.html#os.getenv
# Accepts "true", "1", "yes", "on" (case-insensitive) as truthy.
DATABASE_INTEGRATION_ENABLED: bool = os.getenv(
    "DATABASE_INTEGRATION_ENABLED", "true"
).lower() in ("true", "1", "yes", "on")

# --- Schema Reset Control ---
# Set SUMANAL_ALLOW_SCHEMA_RESET=1 to allow destructive schema reset on version mismatch.
# WARNING: This drops ALL data on every restart if the schema version changes. Defaults to 0 (disabled).

# --- Telegram Push Notifications (Optional) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
# TELEGRAM_USER_ID is now dynamically bound via startup handshake.

# --- Browser / Chrome Data Dir ---
CHROME_USER_DATA_DIR: str = os.getenv("CHROME_USER_DATA_DIR", "chrome_profile")

# --- Browser SoM Configuration ---
BROWSER_SOM_HTML_CHAR_BUDGET: int = int(os.getenv("BROWSER_SOM_HTML_CHAR_BUDGET", "20000"))
BROWSER_SOM_TAGS_TO_MARK: str = os.getenv("BROWSER_SOM_TAGS_TO_MARK", "standard_html")
BROWSER_SOM_SCALE_FACTOR: float = float(os.getenv("BROWSER_SOM_SCALE_FACTOR", "1.0"))

# --- Telemetry / Logging ---
TELEMETRY_DRY_RUN: bool = os.getenv("TELEMETRY_DRY_RUN", "false").lower() in ("true", "1", "yes")

# --- Job Watchdog Settings ---
JOB_WATCH_INTERVAL_SECONDS: int = int(os.getenv("JOB_WATCH_INTERVAL_SECONDS", "300"))
JOB_STALE_THRESHOLD_SECONDS: int = int(os.getenv("JOB_STALE_THRESHOLD_SECONDS", str(8 * 3600)))
MAX_RESUME_ATTEMPTS: int = int(os.getenv("MAX_RESUME_ATTEMPTS", "3"))

# --- Artifacts Root ---
ANYTHINGLLM_ARTIFACTS_DIR: str | None = os.getenv("ANYTHINGLLM_ARTIFACTS_DIR")

# --- Azure OpenAI (for LLM client) ---
AZURE_OPENAI_KEY: str | None = os.getenv("AZURE_OPENAI_KEY")
AZURE_KEY: str | None = os.getenv("AZURE_KEY") or AZURE_OPENAI_KEY
AZURE_OPENAI_ENDPOINT: str | None = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_ENDPOINT: str | None = os.getenv("AZURE_ENDPOINT") or AZURE_OPENAI_ENDPOINT
AZURE_DEPLOYMENT: str = os.getenv("AZURE_DEPLOYMENT", "gpt-5.4-mini")

# --- Chutes ---
CHUTES_API_TOKEN: str | None = os.getenv("CHUTES_API_TOKEN")
CHUTES_KEY: str | None = os.getenv("CHUTES_KEY") or CHUTES_API_TOKEN
CHUTES_MODEL: str = os.getenv("CHUTES_MODEL", "meta-llama/Llama-3.3-70B-Instruct")

# --- Snowflake ---
SNOWFLAKE_ACCOUNT: str | None = os.getenv("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_USER: str | None = os.getenv("SNOWFLAKE_USER")
SNOWFLAKE_WAREHOUSE: str | None = os.getenv("SNOWFLAKE_WAREHOUSE")
SNOWFLAKE_DATABASE: str | None = os.getenv("SNOWFLAKE_DATABASE")
SNOWFLAKE_SCHEMA: str | None = os.getenv("SNOWFLAKE_SCHEMA")
SNOWFLAKE_PRIVATE_KEY_PATH: str = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH", "snowflake_private_key.p8")

# --- EDGAR Configuration ---
EDGAR_IDENTITY: str | None = os.getenv("EDGAR_IDENTITY")

# --- Google ---
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")

# Logger agent / debugger configuration removed — drifting legacy values.
# LOGGER_TRUNCATION_LIMIT is still honored by log formatters via getattr on _log_config.

# --- AnythingLLM Integration ---
ANYTHINGLLM_API_KEY: str = os.getenv("ANYTHINGLLM_API_KEY", "YEZTCHW-KHT4C1J-GP8DPW5-SK77TE5")
ANYTHINGLLM_BASE_URL: str = os.getenv("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
ANYTHINGLLM_WORKSPACE_SLUG: str = os.getenv("ANYTHINGLLM_WORKSPACE_SLUG", "my-workspace")
ANYTHINGLLM_CALLBACK_TIMEOUT: int = int(os.getenv("ANYTHINGLLM_CALLBACK_TIMEOUT", "120"))
ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS: int = int(os.getenv("ANYTHINGLLM_CALLBACK_RETRY_DELAY_SECONDS", "30"))

# --- Telegram Destination Routing ---
TELEGRAM_BRIEFING_CHAT_ID: str | None = os.getenv("TELEGRAM_BRIEFING_CHAT_ID")
TELEGRAM_ARCHIVE_CHAT_ID: str | None = os.getenv("TELEGRAM_ARCHIVE_CHAT_ID")
TELEGRAM_MESSAGE_DELAY: float = float(os.getenv("TELEGRAM_MESSAGE_DELAY", "3.1"))
TELEGRAM_MAX_MESSAGE_LENGTH: int = int(os.getenv("TELEGRAM_MAX_MESSAGE_LENGTH", "4000"))
TELEGRAM_MAX_RETRY_AFTER: int = int(os.getenv("TELEGRAM_MAX_RETRY_AFTER", "120"))

# --- Telegram Rate Limiter Configuration ---
TELEGRAM_RATELIMIT_OVERALL_MAX: int = int(os.getenv("TELEGRAM_RATELIMIT_OVERALL_MAX", "28"))
TELEGRAM_RATELIMIT_OVERALL_PERIOD: float = float(os.getenv("TELEGRAM_RATELIMIT_OVERALL_PERIOD", "1.0"))
TELEGRAM_RATELIMIT_GROUP_MAX: int = int(os.getenv("TELEGRAM_RATELIMIT_GROUP_MAX", "18"))
TELEGRAM_RATELIMIT_GROUP_PERIOD: float = float(os.getenv("TELEGRAM_RATELIMIT_GROUP_PERIOD", "60.0"))

# --- Batch Reader / Hybrid Search ---
BATCH_READER_VECTOR_WEIGHT: float = float(os.getenv("BATCH_READER_VECTOR_WEIGHT", "0.6"))
BATCH_READER_KEYWORD_WEIGHT: float = float(os.getenv("BATCH_READER_KEYWORD_WEIGHT", "0.4"))

# --- Context Limits & Truncation ---
LLM_CONTEXT_CHAR_LIMIT: int = int(os.getenv("LLM_CONTEXT_CHAR_LIMIT", "800000"))
CALLBACK_TRUNCATION_MULTIPLIER: float = float(os.getenv("CALLBACK_TRUNCATION_MULTIPLIER", "0.5"))

# --- Backup Configuration ---
# Backup is now fully managed via pydantic-settings in database/backup/settings.py
