# database/schemas/sync_audit.py

TABLES = {
    "sync_runs": """CREATE TABLE IF NOT EXISTS sync_runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        state TEXT NOT NULL DEFAULT 'STARTED'
            CHECK(state IN ('STARTED','METRICS_COLLECTED','RECOMMENDED','DECIDED',
                            'APPLYING','PUSHING','COMPLETED','FAILED','PARTIAL','ABORTED')),
        metrics_json TEXT,
        recommendation_json TEXT,
        final_strategy TEXT,
        decision_source TEXT,
        overrode_recommendation INTEGER,
        error_message TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_sync_runs_state ON sync_runs(state, started_at);
    """,
    "strategy_decisions": """CREATE TABLE IF NOT EXISTS strategy_decisions (
        decision_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        table_name TEXT NOT NULL,
        recommended_strategy TEXT NOT NULL,
        final_strategy TEXT NOT NULL,
        decision_source TEXT NOT NULL,
        confidence REAL NOT NULL,
        overrode_recommendation INTEGER NOT NULL,
        decided_at TEXT NOT NULL,
        reasoning_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_strategy_decisions_run ON strategy_decisions(run_id, table_name);
    """,
}
