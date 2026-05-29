# database/backup/base_store.py
import json
import os
import tempfile
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, timezone
from database.writer import enqueue_transaction
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class JsonStore(ABC):
    def __init__(self, backup_dir: Path, sub_dir: str, manifest_filename: str):
        self.backup_dir = backup_dir / sub_dir
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.backup_dir.parent / manifest_filename
        self.manifest = self._load_manifest()

    @property
    @abstractmethod
    def entity_key(self) -> str: pass

    @property
    @abstractmethod
    def manifest_entity_key(self) -> str: pass

    @abstractmethod
    def build_upsert_statements(self, entity_id: str, data: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]: pass

    @abstractmethod
    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]: pass

    @abstractmethod
    def get_all_from_sqlite(self, conn) -> List[dict]: pass

    def _load_manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if self.manifest_entity_key not in data:
                        data[self.manifest_entity_key] = {}
                    return data
            except Exception as e:
                log.dual_log(tag=f"Store:Manifest:Corrupt", level="WARNING", message=f"Manifest corrupt: {e}", payload={"error": str(e)})
        return {self.manifest_entity_key: {}, "last_synced_at": None}

    def _save_manifest(self) -> None:
        tmp_path = self.manifest_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, separators=(",", ":"), ensure_ascii=False)
            os.replace(tmp_path, self.manifest_path)
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def _atomic_write(self, path: Path, content: bytes, mode: str = "wb") -> None:
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_")
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

    def _read_json(self, entity_id: str) -> Optional[dict]:
        json_path = self.backup_dir / f"{entity_id}.json"
        if not json_path.exists():
            return None
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _read_bin(self, entity_id: str) -> Optional[bytes]:
        bin_path = self.backup_dir / f"{entity_id}.bin"
        if not bin_path.exists():
            return None
        try:
            return bin_path.read_bytes()
        except Exception:
            return None

    def mark_synced(self) -> None:
        self.manifest["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        self._save_manifest()

    def upsert_entity(self, entity_id: str, data: dict, bin_data: Optional[bytes] = None) -> None:
        updated_at = data.get("updated_at", datetime.now(timezone.utc).isoformat())
        data["updated_at"] = updated_at

        json_path = self.backup_dir / f"{entity_id}.json"
        json_content = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._atomic_write(json_path, json_content)

        if bin_data:
            bin_path = json_path.with_suffix(".bin")
            self._atomic_write(bin_path, bin_data)
            import hashlib
            data["checksum"] = hashlib.sha256(bin_data).hexdigest()
        else:
            bin_path = json_path.with_suffix(".bin")
            if bin_path.exists():
                try:
                    bin_path.unlink()
                except Exception:
                    pass
            data.pop("checksum", None)

        self.manifest[self.manifest_entity_key][entity_id] = {
            "updated_at": updated_at,
            "checksum": data.get("checksum"),
        }
        self._save_manifest()

        stmts = self.build_upsert_statements(entity_id, data, bin_data)
        if stmts:
            enqueue_transaction(stmts)

    def delete_entity(self, entity_id: str) -> None:
        json_path = self.backup_dir / f"{entity_id}.json"
        bin_path = json_path.with_suffix(".bin")
        for p in [json_path, bin_path]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

        removed = self.manifest[self.manifest_entity_key].pop(entity_id, None)
        if removed:
            self._save_manifest()

        stmts = self.build_delete_statements(entity_id)
        if stmts:
            enqueue_transaction(stmts)

    def export_from_sqlite(self, conn) -> dict:
        """Export all SQLite rows for this entity type into JSON/Bin files."""
        try:
            rows = self.get_all_from_sqlite(conn)
        except Exception as e:
            log.dual_log(
                tag="Store:Export:SqliteError",
                level="ERROR",
                message=f"Failed to query SQLite for export: {e}",
                payload={"error": str(e)}
            )
            return {"exported": 0}

        for row in rows:
            row_dict = dict(row)
            entity_id = str(row_dict[self.entity_key])
            bin_data = None
            if "embedding" in row_dict and isinstance(row_dict["embedding"], bytes):
                bin_data = row_dict["embedding"]
            self.upsert_entity(entity_id, row_dict, bin_data)

        return {"exported": len(rows)}

    def cleanup_orphaned_files(self, conn) -> dict:
        """Deletes JSON/Bin files for entities that no longer exist in SQLite (fixes resurrection bug)."""
        try:
            rows = self.get_all_from_sqlite(conn)
            sqlite_ids = {str(row[self.entity_key]) for row in rows}
        except Exception:
            return {"deleted": 0}

        deleted_count = 0
        for file_path in self.backup_dir.glob("*.json"):
            entity_id = file_path.stem
            if entity_id not in sqlite_ids:
                try:
                    file_path.unlink()
                    bin_path = file_path.with_suffix(".bin")
                    if bin_path.exists():
                        bin_path.unlink()
                    self.manifest[self.manifest_entity_key].pop(entity_id, None)
                    deleted_count += 1
                except Exception:
                    pass
                    
        if deleted_count > 0:
            self._save_manifest()
            
        return {"deleted": deleted_count}

    def reconcile(self, conn) -> dict:
        """Generic delta reconciliation between manifest and SQLite."""
        manifest_entities = self.manifest.get(self.manifest_entity_key, {})
        manifest_ids = set(manifest_entities.keys())

        try:
            sqlite_rows = self.get_all_from_sqlite(conn)
        except Exception as e:
            log.dual_log(
                tag="Store:Reconcile:SqliteError",
                level="ERROR",
                message=f"Failed to query SQLite for reconciliation: {e}",
                payload={"error": str(e)},
            )
            return {"deletes": 0, "inserts": 0, "updates": 0, "errors": 1}

        sqlite_dict = {str(row[self.entity_key]): row.get("updated_at", "") for row in sqlite_rows}
        sqlite_ids = set(sqlite_dict.keys())

        ops: List[Tuple[str, tuple]] = []
        summary = {"deletes": 0, "inserts": 0, "updates": 0, "errors": 0}
        ghosts_purged = False

        # Rule 1: SQLite has ID, manifest doesn't -> DELETE from SQLite
        for eid in sqlite_ids - manifest_ids:
            ops.extend(self.build_delete_statements(eid))
            summary["deletes"] += 1

        # Rule 2: Manifest has ID, SQLite doesn't -> INSERT into SQLite
        for eid in manifest_ids - sqlite_ids:
            json_path = self.backup_dir / f"{eid}.json"
            if not json_path.exists():
                self.manifest[self.manifest_entity_key].pop(eid, None)
                ghosts_purged = True
                summary["errors"] += 1
                continue

            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue

            bin_path = json_path.with_suffix(".bin")
            emb = None
            if bin_path.exists():
                try:
                    emb = bin_path.read_bytes()
                except Exception:
                    pass

            ops.extend(self.build_upsert_statements(eid, meta, emb))
            summary["inserts"] += 1

        # Rule 3: Both exist, manifest newer -> UPDATE SQLite
        for eid in manifest_ids & sqlite_ids:
            manifest_updated = manifest_entities[eid].get("updated_at", "")
            sqlite_updated = sqlite_dict.get(eid, "")
            if manifest_updated > sqlite_updated:
                json_path = self.backup_dir / f"{eid}.json"
                if not json_path.exists():
                    self.manifest[self.manifest_entity_key].pop(eid, None)
                    ghosts_purged = True
                    summary["errors"] += 1
                    continue

                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    continue

                bin_path = json_path.with_suffix(".bin")
                emb = None
                if bin_path.exists():
                    try:
                        emb = bin_path.read_bytes()
                    except Exception:
                        pass

                ops.extend(self.build_upsert_statements(eid, meta, emb))
                summary["updates"] += 1

        if ghosts_purged:
            self._save_manifest()
            log.dual_log(tag="Store:Reconcile:GhostPurge", level="INFO", message="Purged ghost entries from manifest", payload={"purged": True})

        if ops:
            enqueue_transaction(ops)

        self.manifest["last_synced_at"] = datetime.now(timezone.utc).isoformat()
        self._save_manifest()

        return summary
