# database/backup/sync/user_confirmation.py
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
        from database.backup.sync.strategy_recommender import StrategyRecommender
        rec = StrategyRecommender.recommend(metrics['tables'], metrics['local_enabled'], metrics['cloud_enabled'])
        print(f"\n>>> RECOMMENDED: {rec.overall_strategy.value} (confidence: {rec.overall_confidence:.0%})")
        print(f">>> Reasoning: {rec.reasoning}")
        
        from utils.logger.state import hitl_buffer_lock
        import utils.logger.state as log_state
        with hitl_buffer_lock:
            log_state.hitl_buffering_active = True

        try:
            choice = input(f"Strategy [N/O/L/C/A] (recommended: {rec.overall_strategy.value}): ").strip().upper()
        except EOFError:
            choice = "A"
            if rec.safe_to_auto_accept:
                choice_map = {
                    "newest_wins": "N",
                    "operational_wins": "O",
                    "local_backup_wins": "L",
                    "cloud_backup_wins": "C",
                }
                choice = choice_map.get(rec.overall_strategy.value, "A")

        with hitl_buffer_lock:
            log_state.hitl_buffering_active = False
            to_flush = list(log_state.hitl_buffer)
            log_state.hitl_buffer.clear()
        
        for logger_inst, t, m, lvl, p, exc, st in to_flush:
            logger_inst.dual_log(tag=t, message=m, level=lvl, payload=p, exc_info=exc, status_state=st)

        if choice == 'N': return 'newest_overall_wins'
        if choice == 'O': return 'operational_wins'
        if choice == 'L' or choice == 'B': return 'backup_wins'
        if choice == 'C': return 'cloud_backup_wins'
        if choice == 'A': return 'abort'
