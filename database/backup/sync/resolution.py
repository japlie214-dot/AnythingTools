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
        from database.backup.sync.smart_recommender import SmartRecommender
        recommender = SmartRecommender()
        rec = recommender.recommend(metrics)

        print("\n=== BIDIRECTIONAL SYNC METRICS ===")
        print(recommender.format_outcomes_display(rec))

        print("\n=== STRATEGY DICTIONARY ===")
        print("  N = newest_overall_wins (Merge bidirectionally. Conflicts resolved by newest timestamp.)")
        print("  O = operational_wins (Operational DB overrides local backup)")
        print("  L = local_backup_wins (Local backup overrides operational)")
        print("  C = cloud_backup_wins (Cloud backup overrides both operational and local)")
        print("  A = abort (Cancel sync)")
        
        rec_key = {"operational_wins": "O", "local_backup_wins": "L", "cloud_backup_wins": "C", "newest_overall_wins": "N", "abort": "A"}.get(rec.strategy, "A")
        print(f"\n  RECOMMENDED: {rec_key} ({rec.strategy}) — {int(rec.confidence * 100)}% confidence")
        print(f"  Reason: {rec.reasoning}")

        from utils.logger.state import hitl_buffer_lock
        import utils.logger.state as log_state
        with hitl_buffer_lock:
            log_state.hitl_buffering_active = True

        try:
            choice = input(f"\n>>> Strategy [N/O/L/C/A] (recommended: {rec_key}): ").strip().upper()
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
        if choice in ('L', 'B'): return 'local_backup_wins'
        if choice == 'C': return 'cloud_backup_wins'
        return 'abort'
