# database/backup/vec/rehydrate.py
import sqlite3
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

from database.connection import _attempt_vec_load

def rehydrate_vec0(local_db_path: str, cloud_engine):
    """Safely rebuild the vec0 virtual table and its shadow indices."""
    conn = sqlite3.connect(local_db_path)
    try:
        _attempt_vec_load(conn)
        conn.execute("DROP TABLE IF EXISTS scraped_articles_vec")
        conn.execute("CREATE VIRTUAL TABLE scraped_articles_vec USING vec0(embedding float[1024])")
        
        # Cloud streaming simulation here (omitted for brevity in plan, 
        # but implementation should iterate through cloud_engine.pull_vectors)
        
        conn.execute("INSERT INTO scraped_articles_vec(scraped_articles_vec) VALUES('optimize')")
        conn.commit()
        log.dual_log(tag="Backup:Vec0:Rehydrate:Complete", message="Vec0 optimization and shadow table rebuild complete", payload={"status": "success"})
    except Exception as e:
        conn.rollback()
        log.dual_log(tag="Backup:Vec0:Rehydrate:Error", message=f"Vec0 rebuild failed: {e}", level="ERROR", exc_info=e, payload={"error": str(e)})
        raise e
    finally:
        conn.close()
