# config.py — AnythingTools configuration (safe defaults)
import os
from pathlib import Path

# --- API Security ---
API_KEY: str = os.getenv("API_KEY", "dev_default_key_change_me_in_production")
ANYTHINGTOOLS_PORT: int = int(os.getenv("ANYTHINGTOOLS_PORT", "8000"))

# --- Telegram Push Notifications (Optional) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID: str | None = os.getenv("TELEGRAM_USER_ID")

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
AZURE_OPENAI_ENDPOINT: str | None = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_DEPLOYMENT: str = os.getenv("AZURE_DEPLOYMENT", "gpt-5.4-mini")

# --- Logger Agent Context ---
LOGGER_AGENT_MAX_CONTEXT: int = int(os.getenv("LOGGER_AGENT_MAX_CONTEXT", "100000"))
DEBUGGER_AGENT_TRIGGER_ON_WARNING: bool = os.getenv("DEBUGGER_AGENT_TRIGGER_ON_WARNING", "true").lower() in ("true", "1", "yes")
# Full‑fidelity payload logging limit (bytes). Aligns with plan to allow up to 5 MB per log entry.
LOGGER_TRUNCATION_LIMIT: int = int(os.getenv("LOGGER_TRUNCATION_LIMIT", "5000000"))
