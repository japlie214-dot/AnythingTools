# config.py — AnythingTools configuration (safe defaults)
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ANYTHINGTOOLS_PORT: int = int(os.getenv("ANYTHINGTOOLS_PORT", "8000"))

# --- Operational Database Path ---
# When DATABASE_STAGING_ENABLED=true, this is overridden to data/staging/sumanal.db
# Ref: https://docs.python.org/3/library/os.html#os.getenv
_staging_enabled: bool = os.getenv(
    "DATABASE_STAGING_ENABLED", "false"
).lower() in ("true", "1", "yes", "on")

DATABASE_STAGING_ENABLED: bool = _staging_enabled
# When true, TRUNCATE staging tables on app startup and shutdown.
# Default true for local dev. Set false in cloud deployments to avoid
# concurrent containers truncating each other's staging data.
DATABASE_STAGING_WIPE_ON_STARTUP: bool = os.getenv("DATABASE_STAGING_WIPE_ON_STARTUP", "true").lower() in ("true", "1", "yes", "on")

# The base operational DB path. If staging is enabled, the actual path
# is resolved in database/connection.py to data/staging/sumanal.db.
OPERATIONAL_DB_PATH: str = os.getenv("OPERATIONAL_DB_PATH", "data/sumanal.db")

# --- Database Integration Master Toggle ---
# When False, operational DB writes (enqueue_write, enqueue_transaction,
# enqueue_execscript) and Snowflake cloud writes are skipped.
# Startup phases init_database_layer, run_db_migrations, validate_vec0,
# init_backup, sync_from_backup become no-ops.
#
# logs.db writes are NOT affected by this toggle — logs.db always writes
# so observability is never lost. See database/logs_writer.py.
#
# This is DISTINCT from BACKUP_CLOUD_ENABLED (in database/backup/settings.py):
#   - BACKUP_CLOUD_ENABLED=false → cloud sync disabled, SQLite still writes.
#   - DATABASE_INTEGRATION_ENABLED=false → operational DB + cloud disabled.
#   - DATABASE_STAGING_ENABLED=true → diverts file paths to data/staging/,
#     overriding the above. Staging always wins.
#
# Per Python os.getenv docs: https://docs.python.org/3/library/os.html#os.getenv
DATABASE_INTEGRATION_ENABLED: bool = os.getenv(
    "DATABASE_INTEGRATION_ENABLED", "true"
).lower() in ("true", "1", "yes", "on")

# --- Telegram Push Notifications (Optional) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")

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
# Decoupled from AnythingLLM. Tools write audit/debug artifacts here.
# When DATABASE_STAGING_ENABLED=true, this defaults to data/staging/artifacts/.
ARTIFACTS_DIR: str = os.getenv(
    "ARTIFACTS_DIR",
    "data/staging/artifacts" if DATABASE_STAGING_ENABLED else "data/artifacts"
)

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

# --- Sync API Configuration ---
# Maximum wall-clock seconds for a sync API request before returning 504.
# Set to 290 (10s under Cloud Run's default 300s timeout) to give the
# client headroom to receive the 504 response.
# Ref: https://docs.cloud.google.com/run/docs/configuring/request-timeout
SYNC_API_TIMEOUT_SECONDS: int = int(os.getenv("SYNC_API_TIMEOUT_SECONDS", "290"))

# Maximum concurrent sync-held connections. Prevents resource exhaustion
# under WEB_CONCURRENCY=1. Ref: https://docs.python.org/3/library/asyncio-sync.html#asyncio.Semaphore
SYNC_MAX_CONCURRENT_JOBS: int = int(os.getenv("SYNC_MAX_CONCURRENT_JOBS", "20"))

# --- Activity-Driven Observability Configuration ---
# Per-key character cap for lineage inputs/outputs. Default 50,000 per the
# Developer Contract. Override only with explicit justification.
# Ref: Developer Contract in utils/observability/__init__.py §4.3.d
LINEAGE_MAX_STRING_CHARS: int = int(os.getenv("LINEAGE_MAX_STRING_CHARS", "50000"))

# Maximum activities recorded per job. Beyond this, activities are dropped
# with a dropped_count marker in the lineage summary.
LINEAGE_MAX_ACTIVITIES: int = int(os.getenv("LINEAGE_MAX_ACTIVITIES", "1000"))

# Comma-separated list of additional key names to mask in lineage.
# Appended to the default blocklist in utils/observability/masking.py.
LINEAGE_EXTRA_MASK_KEYS: str = os.getenv("LINEAGE_EXTRA_MASK_KEYS", "")

# --- Backup Configuration ---
# Backup is now fully managed via pydantic-settings in database/backup/settings.py
