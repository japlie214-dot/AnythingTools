# database/schemas/token.py

TABLES = {
    "token_usage": """CREATE TABLE token_usage (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            telemetry_id TEXT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK(prompt_tokens >= 0),
            completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK(completion_tokens >= 0),
            reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK(reasoning_tokens >= 0),
            total_tokens INTEGER NOT NULL DEFAULT 0 CHECK(total_tokens >= 0),
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_token_usage_session_recorded ON token_usage(session_id, recorded_at);
""",
}

VEC_TABLES = {
}
