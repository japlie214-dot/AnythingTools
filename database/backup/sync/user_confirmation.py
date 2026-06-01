# database/backup/sync/user_confirmation.py
class UserConfirmationHandler:
    @staticmethod
    def prompt_for_conflict(table_name: str, row_id: str, local_ts: str, cloud_ts: str) -> str:
        print(f"\n[!!!] SYNC CONFLICT DETECTED in table '{table_name}' for row '{row_id}'")
        print(f"Local updated_at: {local_ts}")
        print(f"Cloud updated_at: {cloud_ts}")
        while True:
            choice = input("Keep [L]ocal, Keep [C]loud, or [S]kip? ").strip().upper()
            if choice == 'L': return 'local'
            if choice == 'C': return 'cloud'
            if choice == 'S': return 'skip'
