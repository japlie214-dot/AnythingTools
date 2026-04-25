# tools/backup/config.py
from pathlib import Path
from dataclasses import dataclass
import config as global_config

@dataclass(frozen=True)
class BackupConfig:
    """Immutable backup configuration. All paths are resolved absolutes."""

    enabled: bool
    backup_dir: Path
    batch_size: int
    compression: str

    @property
    def articles_dir(self) -> Path:
        return self.backup_dir / "articles"

    @property
    def vectors_dir(self) -> Path:
        return self.backup_dir / "vectors"

    @property
    def watermark_path(self) -> Path:
        return self.backup_dir / "watermark.json"

    def ensure_dirs(self) -> None:
        self.articles_dir.mkdir(parents=True, exist_ok=True)
        self.vectors_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_global_config(cls) -> "BackupConfig":
        backup_dir_str = getattr(global_config, "BACKUP_ONEDRIVE_DIR", "")
        if backup_dir_str:
            backup_dir = Path(backup_dir_str).resolve()
        else:
            backup_dir = Path("backups").resolve()

        return cls(
            enabled=getattr(global_config, "BACKUP_ENABLED", True),
            backup_dir=backup_dir,
            batch_size=getattr(global_config, "BACKUP_BATCH_SIZE", 1000),
            compression=getattr(global_config, "BACKUP_COMPRESSION", "zstd"),
        )
