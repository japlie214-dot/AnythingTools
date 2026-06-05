# database/backup/sync/conflict_resolver.py
from typing import Dict, Literal

class ConflictResolver:
    @staticmethod
    def resolve_triad(conflict_row: Dict[str, str], strategy: str = "newest_overall_wins") -> str:
        op_ts = conflict_row.get("op_ts", "")
        bk_ts = conflict_row.get("bk_ts", "")
        classification = conflict_row.get("classification", "")
        
        if strategy == "operational_wins":
            return "operational"
        elif strategy in ("local_backup_wins", "cloud_backup_wins"):
            return "backup"
        elif strategy == "newest_overall_wins":
            if classification == "timestamp_drift":
                return "operational" if op_ts > bk_ts else "backup"
            if op_ts > bk_ts:
                return "operational"
            elif bk_ts > op_ts:
                return "backup"
        return "manual"
