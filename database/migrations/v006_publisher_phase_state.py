# database/migrations/v006_publisher_phase_state.py

import sqlite3
import json

version = 6
description = "Deprecate posted_*_ulids and add phase_state to broadcast_batches"

def up(conn: sqlite3.Connection, sqlite_vec_available: bool) -> None:
    """
    Migrate broadcast_batches table to use phase_state JSON instead of legacy array columns.
    
    This migration:
    1. Creates a new table with phase_state JSON column
    2. Translates existing posted_research_ulids and posted_summary_ulids into phase_state structure
    3. Swaps tables and restores indexes
    """
    
    # 1. Create new table without the legacy columns
    conn.execute("""
        CREATE TABLE broadcast_batches_new (
            batch_id TEXT PRIMARY KEY,
            target_site TEXT NOT NULL,
            raw_json_path TEXT NOT NULL,
            curated_json_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','PUBLISHING','PARTIAL','COMPLETED','FAILED')),
            phase_state TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Extract and translate data from old table
    rows = conn.execute("SELECT * FROM broadcast_batches").fetchall()
    
    for row in rows:
        r_dict = dict(row)
        phase_state = {
            "validate": {},
            "translate": {},
            "publish_briefing": {},
            "publish_archive": {}
        }
        
        try:
            # Migrate posted_research_ulids -> publish_briefing phase
            briefing_ulids = json.loads(r_dict.get("posted_research_ulids") or "[]")
            for ulid in briefing_ulids:
                phase_state["publish_briefing"][ulid] = {"status": "COMPLETED"}
                
            # Migrate posted_summary_ulids -> publish_archive phase
            archive_ulids = json.loads(r_dict.get("posted_summary_ulids") or "[]")
            for ulid in archive_ulids:
                phase_state["publish_archive"][ulid] = {"status": "COMPLETED"}
        except Exception:
            # Fallback to empty phase state on corrupt JSON
            pass
            
        phase_state_json = json.dumps(phase_state, ensure_ascii=False)
        
        conn.execute("""
            INSERT INTO broadcast_batches_new 
            (batch_id, target_site, raw_json_path, curated_json_path, status, phase_state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r_dict["batch_id"], r_dict["target_site"], r_dict["raw_json_path"], 
            r_dict["curated_json_path"], r_dict["status"], phase_state_json, 
            r_dict["created_at"], r_dict["updated_at"]
        ))

    # 3. Swap tables and restore indexes
    conn.execute("DROP TABLE broadcast_batches")
    conn.execute("ALTER TABLE broadcast_batches_new RENAME TO broadcast_batches")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_broadcast_batches_status ON broadcast_batches(status)")
