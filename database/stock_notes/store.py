# database/stock_notes/store.py
import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class FilingStore:
    """Manages per-filing backup files, manifest, and JSON payload coordination."""

    def __init__(self, backup_dir: Path):
        self.backup_dir = backup_dir / "stock_notes"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.backup_dir.parent / "stock_notes_manifest.json"
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict:
        """Load manifest from disk, resolving corruption with a fresh dict."""
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "filings" not in data:
                        data["filings"] = {}
                    if "last_synced_at" not in data:
                        data["last_synced_at"] = None
                    return data
            except (json.JSONDecodeError, OSError) as e:
                log.dual_log(
                    tag="FilingStore:Manifest:Corrupt",
                    level="WARNING",
                    message=f"Manifest corrupt or unreadable, starting fresh: {e}",
                    payload={"path": str(self.manifest_path), "error": str(e)},
                )
        return {"filings": {}, "last_synced_at": None}

    def _save_manifest(self) -> None:
        """Atomic manifest write using tempfile."""
        tmp_path = self.manifest_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp_path, self.manifest_path)
        except Exception as e:
            log.dual_log(
                tag="FilingStore:Manifest:Write",
                level="ERROR",
                message=f"Failed to write manifest: {e}",
                payload={"error": str(e)},
            )
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: str = "wb") -> None:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_"
        )
        try:
            with os.fdopen(fd, mode) as f:
                f.write(content)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise

    def upsert_filing_payload(
        self,
        accession_no: str,
        payload: Dict[str, Any],
    ) -> None:
        """Create or update a filing JSON payload atomically."""
        updated_at = datetime.now(timezone.utc).isoformat()
        
        json_path = self.backup_dir / f"{accession_no}.json"
        json_content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._atomic_write(json_path, json_content)

        self.manifest["filings"][accession_no] = {
            "updated_at": updated_at,
        }
        self._save_manifest()

        log.dual_log(
            tag="FilingStore:Upsert:Success",
            level="INFO",
            message=f"Upserted filing payload {accession_no}",
            payload={"accession_no": accession_no, "updated_at": updated_at},
        )

# ── Global Singleton ──
_global_store: Optional[FilingStore] = None
_global_store_lock = __import__("threading").Lock()

def get_filing_store() -> FilingStore:
    global _global_store
    with _global_store_lock:
        if _global_store is None:
            from database.backup.config import BackupConfig
            config = BackupConfig.from_global_config()
            _global_store = FilingStore(config.backup_dir)
        return _global_store
