# database/backup/sync/conflict_resolver.py
from typing import Dict, Literal

class ConflictResolver:
    @staticmethod
    def resolve_triad(conflict_row: Dict[str, str], strategy: str = "newest_overall_wins") -> str:
        op_ts = conflict_row.get("operational_ts", "")
        bk_ts = conflict_row.get("backup_ts", "")
        
        if strategy == "operational_wins":
            return "operational"
        elif strategy in ("local_backup_wins", "cloud_backup_wins"):
            return "backup"
        elif strategy == "newest_overall_wins":
            if op_ts > bk_ts:
                return "operational"
            elif bk_ts > op_ts:
                return "backup"
            else:
                return "manual"
        return "manual"
