# database/backup/sync/resolution.py
from typing import Dict

class ConflictResolver:
    @staticmethod
    def resolve_triad(conflict_row: Dict[str, str], strategy: str = "newest_overall_wins") -> str:
        op_ts = conflict_row.get("op_ts", "")
        bk_ts = conflict_row.get("bk_ts", "")
        classification = conflict_row.get("classification", "")

        if strategy == "operational_wins":
            return "operational"
        elif strategy in ("local_backup_wins", "cloud_backup_wins", "backup_wins"):
            return "backup"
        elif strategy == "newest_overall_wins":
            if classification == "timestamp_drift":
                return "operational" if op_ts > bk_ts else "backup"
            if op_ts > bk_ts:
                return "operational"
            elif bk_ts > op_ts:
                return "backup"
        return "manual"

class UserConfirmationHandler:
    @staticmethod
    def hitl_wait_for_sync_operator(table_name: str, row_id: str, op_ts: str, bk_ts: str, cloud_ts: str) -> str:
        print(f"\n\n[!!!] HITL SYNC CONFLICT ALERT")
        print(f">>> Table: {table_name} | Row ID: {row_id}")
        print(f">>> Operational Timestamp: {op_ts}")
        print(f">>> Local Backup Timestamp: {bk_ts}")
        print(f">>> Cloud Backup Timestamp: {cloud_ts}")
        print(">>> Type 'O' (Keep Operational), 'B' (Keep Backup), or 'S' (Skip to Dead Letter Queue).")

        try:
            choice = input("Decision [O/B/S]: ").strip().upper()
        except EOFError:
            choice = "S"

        if choice == 'O': return 'operational'
        if choice == 'B': return 'backup'
        return 'skip'

    @staticmethod
    def hitl_prompt_sync_strategy(metrics: dict) -> str:
        print("\n=== BIDIRECTIONAL SYNC METRICS ===")
        tables = metrics.get('tables', {})
        for tbl, m in tables.items():
            print(f"  {tbl}: op={m.get('op_rows')} bk={m.get('bk_rows')} op_only={m.get('op_only')} bk_only={m.get('bk_only')} conflicts={m.get('conflicts')} identical={len(m.get('content_identical', []))}")
            
        print("\n=== STRATEGY DICTIONARY ===")
        print("  N = newest_overall_wins (merge bidirectionally)")
        print("  O = operational_wins (op overrides backup)")
        print("  L = backup_wins (backup overrides op)")
        print("  C = cloud_backup_wins (cloud overrides both)")
        print("  A = abort")
        
        total_conflicts = sum(len(t.get('genuine_conflicts', [])) for t in tables.values())
        recommended = "N" if total_conflicts == 0 else "A"
        
        from utils.logger.state import hitl_buffer_lock
        import utils.logger.state as log_state
        
        with hitl_buffer_lock:
            log_state.hitl_buffering_active = True

        try:
            choice = input(f"\n>>> Strategy [N/O/L/C/A] (recommended: {recommended}): ").strip().upper()
        except EOFError:
            choice = "A"

        with hitl_buffer_lock:
            log_state.hitl_buffering_active = False
            to_flush = list(log_state.hitl_buffer)
            log_state.hitl_buffer.clear()
        
        for logger_inst, t, m, lvl, p, exc, st in to_flush:
            logger_inst.dual_log(tag=t, message=m, level=lvl, payload=p, exc_info=exc, status_state=st)

        if choice == 'N': return 'newest_overall_wins'
        if choice == 'O': return 'operational_wins'
        if choice in ('L', 'B'): return 'backup_wins'
        if choice == 'C': return 'cloud_backup_wins'
        return 'abort'