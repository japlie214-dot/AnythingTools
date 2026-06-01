# database/backup/sync/conflict_resolver.py
from typing import Dict, Literal

class ConflictResolver:
    @staticmethod
    def resolve(conflict_row: Dict[str, str], last_sync_ts: str) -> Literal['local', 'cloud', 'manual']:
        local_ts = conflict_row.get("local_ts", "")
        cloud_ts = conflict_row.get("cloud_ts", "")
        if local_ts > last_sync_ts and cloud_ts <= last_sync_ts:
            return 'local'
        if cloud_ts > last_sync_ts and local_ts <= last_sync_ts:
            return 'cloud'
        return 'manual'
