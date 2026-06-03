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
        print("\n" + "="*80)
        print("=== HITL PRE-SYNC VALIDATION & SOURCE OF TRUTH SELECTION ===")
        print("="*80)
        print(f"Operational DB : {metrics['op_db_path']}")
        print(f"Local Backup   : {metrics['bk_db_path']} (Enabled: {metrics['local_enabled']})")
        print(f"Cloud Backup   : {metrics['cloud_account']} (Enabled: {metrics['cloud_enabled']})")
        print("-" * 80)
        print(f"{'TABLE':<22} | {'OP ROWS':<8} | {'BK ROWS':<8} | {'OP_ONLY':<8} | {'BK_ONLY':<8} | {'CONFLICTS':<9}")
        for t, m in metrics['tables'].items():
            print(f"{t:<22} | {m['op_rows']:<8} | {m['bk_rows']:<8} | {m['op_only']:<8} | {m['bk_only']:<8} | {m['conflicts']:<9}")
            op_latest = str(m.get('op_latest', 'N/A'))[:19]
            bk_latest = str(m.get('bk_latest', 'N/A'))[:19]
            if op_latest != 'N/A' or bk_latest != 'N/A':
                print(f"  └─ Latest Update -> Op: {op_latest} | Bk: {bk_latest}")
        print("="*80)
        print("Select the Source of Truth Strategy for this sync session:")
        print("  [N] Newest Overall Wins (Merge delta, resolve conflicts by latest updated_at)")
        print("  [O] Operational Wins (Force Op state, overwrite backups)")
        print("  [B] Backup Wins (Force Local/Cloud state, overwrite Op)")
        print("  [A] Abort Sync")
        while True:
            try:
                choice = input("Strategy [N/O/B/A]: ").strip().upper()
            except EOFError:
                choice = "A"
            if choice == 'N': return 'newest_overall_wins'
            if choice == 'O': return 'operational_wins'
            if choice == 'B': return 'backup_wins'
            if choice == 'A': return 'abort'
