# database/articles/parquet_stream.py
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict
import pyarrow as pa
import pyarrow.parquet as pq

from database.articles.schema import ARTICLE_SCHEMA, VECTOR_SCHEMA, get_article_row_as_dict, get_vector_row_as_dict
from utils.logger import get_dual_logger
from database.backup.config import BackupConfig

log = get_dual_logger(__name__)

class StreamingParquetWriter:
    def __init__(self, backup_dir: Path):
        self.backup_dir = Path(backup_dir)
        self._lock = threading.Lock()
        self._writers: Dict[str, pq.ParquetWriter] = {}
        self._temp_paths: Dict[str, Path] = {}
        
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        (self.backup_dir / "scraped_articles").mkdir(parents=True, exist_ok=True)
        (self.backup_dir / "scraped_articles_vec").mkdir(parents=True, exist_ok=True)

    def _get_bucket_ts(self) -> str:
        now = datetime.now(timezone.utc)
        bucket_minute = (now.minute // 5) * 5
        bucket_dt = now.replace(minute=bucket_minute, second=0, microsecond=0)
        return bucket_dt.strftime("%Y%m%d_%H%M%S")

    def write_article(self, task) -> Path:
        dest = self.backup_dir / "scraped_articles" / f"scraped_articles_{self._get_bucket_ts()}.parquet"
        temp_path = dest.with_suffix(".tmp.parquet")
        
        with self._lock:
            writer = self._writers.get(str(dest))
            if writer is None:
                writer = pq.ParquetWriter(str(temp_path), ARTICLE_SCHEMA, compression="zstd", version="2.6")
                self._writers[str(dest)] = writer
                self._temp_paths[str(dest)] = temp_path
            
            row_dict = get_article_row_as_dict(task)
            table = pa.Table.from_pydict({k: [v] for k, v in row_dict.items()}, schema=ARTICLE_SCHEMA)
            writer.write_table(table)
            
            log.dual_log(tag="Backup:Storage:Write", level="DEBUG", message=f"Wrote article {task.article_id} to Parquet", payload={"article_id": task.article_id, "dest": str(dest)})
            return dest

    def write_vector(self, task) -> Optional[Path]:
        if not task.embedding_bytes:
            return None
        dest = self.backup_dir / "scraped_articles_vec" / f"scraped_articles_vec_{self._get_bucket_ts()}.parquet"
        temp_path = dest.with_suffix(".tmp.parquet")
        
        with self._lock:
            writer = self._writers.get(str(dest))
            if writer is None:
                writer = pq.ParquetWriter(str(temp_path), VECTOR_SCHEMA, compression="zstd", version="2.6")
                self._writers[str(dest)] = writer
                self._temp_paths[str(dest)] = temp_path
            
            row_dict = get_vector_row_as_dict(task)
            table = pa.Table.from_pydict({k: [v] for k, v in row_dict.items()}, schema=VECTOR_SCHEMA)
            writer.write_table(table)
            return dest

    def flush(self):
        with self._lock:
            for key, writer in list(self._writers.items()):
                writer.close()
                temp = self._temp_paths.get(key)
                if temp and temp.exists():
                    temp.replace(Path(key))
            self._writers.clear()
            self._temp_paths.clear()

_global_writer: Optional[StreamingParquetWriter] = None
_global_lock = threading.Lock()

def get_streaming_writer() -> StreamingParquetWriter:
    global _global_writer
    with _global_lock:
        if _global_writer is None:
            config = BackupConfig.from_global_config()
            _global_writer = StreamingParquetWriter(config.backup_dir)
        return _global_writer
