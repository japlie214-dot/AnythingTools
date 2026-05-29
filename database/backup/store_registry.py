# database/backup/store_registry.py
import threading
from typing import Dict
from database.backup.base_store import JsonStore
from database.backup.config import BackupConfig

class StoreRegistry:
    _stores: Dict[str, JsonStore] = {}
    _lock = threading.Lock()

    @classmethod
    def get_all_stores(cls) -> Dict[str, JsonStore]:
        with cls._lock:
            if not cls._stores:
                cls._init_stores()
            return cls._stores

    @classmethod
    def _init_stores(cls):
        from database.backup.stores.article_store import get_article_store
        from database.backup.stores.broadcast_store import BroadcastBatchStore, BroadcastDetailStore
        from database.backup.stores.sn_store import SnFilingStore, SnNoteStore, SnDetailRegistryStore
        config = BackupConfig.from_global_config()
        
        cls._stores = {
            'scraped_articles': get_article_store(),
            'broadcast_batches': BroadcastBatchStore(config.backup_dir, 'broadcast_batches', 'broadcast_batches_manifest.json'),
            'broadcast_details': BroadcastDetailStore(config.backup_dir, 'broadcast_details', 'broadcast_details_manifest.json'),
            'sn_filings': SnFilingStore(config.backup_dir, 'sn_filings', 'sn_filings_manifest.json'),
            'sn_notes': SnNoteStore(config.backup_dir, 'sn_notes', 'sn_notes_manifest.json'),
            'sn_detail_registry': SnDetailRegistryStore(config.backup_dir, 'sn_detail_registry', 'sn_detail_registry_manifest.json'),
        }
