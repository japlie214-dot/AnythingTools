# config.py — AnythingTools configuration (safe defaults)
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --- API Security ---
API_KEY: str = os.getenv("API_KEY", "dev_default_key_change_me_in_production")
ANYTHINGTOOLS_PORT: int = int(os.getenv("ANYTHINGTOOLS_PORT", "8000"))

# --- Telegram Push Notifications (Optional) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN", "7772650631:AAGg5cT76mj_kWi5_5UnL9q7fbm9iJlXzEw")
# TELEGRAM_USER_ID is now dynamically bound via startup handshake.

# --- Browser / Chrome Data Dir ---
CHROME_USER_DATA_DIR: str = os.getenv("CHROME_USER_DATA_DIR", "chrome_profile")

# --- Telemetry / Logging ---
TELEMETRY_DRY_RUN: bool = os.getenv("TELEMETRY_DRY_RUN", "false").lower() in ("true", "1", "yes")

# --- Job Watchdog Settings ---
JOB_WATCH_INTERVAL_SECONDS: int = int(os.getenv("JOB_WATCH_INTERVAL_SECONDS", "300"))
JOB_STALE_THRESHOLD_SECONDS: int = int(os.getenv("JOB_STALE_THRESHOLD_SECONDS", str(8 * 3600)))

# --- Artifacts Root ---
ARTIFACTS_ROOT: str = os.getenv("ARTIFACTS_ROOT", "artifacts")

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

# --- Google ---
GOOGLE_API_KEY: str | None = os.getenv("GOOGLE_API_KEY")

# Logger agent / debugger configuration removed — drifting legacy values.
# LOGGER_TRUNCATION_LIMIT is still honored by log formatters via getattr on _log_config.

# --- AnythingLLM Integration ---
ANYTHINGLLM_API_KEY: str = os.getenv("ANYTHINGLLM_API_KEY", "YEZTCHW-KHT4C1J-GP8DPW5-SK77TE5")
ANYTHINGLLM_BASE_URL: str = os.getenv("ANYTHINGLLM_BASE_URL", "http://localhost:3001")
ANYTHINGLLM_WORKSPACE_SLUG: str = os.getenv("ANYTHINGLLM_WORKSPACE_SLUG", "my-workspace")

# --- Telegram Destination Routing ---
TELEGRAM_BRIEFING_CHAT_ID: str | None = os.getenv("TELEGRAM_BRIEFING_CHAT_ID", "-1001832461600")
TELEGRAM_ARCHIVE_CHAT_ID: str | None = os.getenv("TELEGRAM_ARCHIVE_CHAT_ID", "-1002574049512")
TELEGRAM_MESSAGE_DELAY: float = float(os.getenv("TELEGRAM_MESSAGE_DELAY", "3.1"))
TELEGRAM_MAX_MESSAGE_LENGTH: int = int(os.getenv("TELEGRAM_MAX_MESSAGE_LENGTH", "4000"))
TELEGRAM_MAX_RETRY_AFTER: int = int(os.getenv("TELEGRAM_MAX_RETRY_AFTER", "120"))
