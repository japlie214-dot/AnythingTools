# database/articles/reconcile.py
from datetime import datetime, timezone
from typing import List, Tuple

from database.connection import DatabaseManager
from database.articles.store import ArticleStore
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

def reconcile_delta(store: ArticleStore) -> dict:
    """Run delta reconciliation between manifest and SQLite."""
    manifest = store.manifest
    manifest_ids = set(manifest["articles"].keys())

    # Get SQLite state
    conn = DatabaseManager.get_read_connection()
    try:
        sqlite_rows = conn.execute(
            "SELECT id, updated_at FROM scraped_articles"
        ).fetchall()
    except Exception as e:
        log.dual_log(
            tag="Article:Reconcile:SqliteError",
            level="ERROR",
            message=f"Failed to query SQLite for reconciliation: {e}",
            payload={"error": str(e)},
        )
        return {"deletes": 0, "inserts": 0, "updates": 0, "errors": 1}

    sqlite_dict = {row["id"]: row["updated_at"] for row in sqlite_rows}
    sqlite_ids = set(sqlite_dict.keys())

    ops: List[Tuple[str, tuple]] = []
    summary = {"deletes": 0, "inserts": 0, "updates": 0, "errors": 0}
    ghosts_purged = False

    # Rule 1: SQLite has ID, manifest doesn't -> DELETE from SQLite
    for aid in sqlite_ids - manifest_ids:
        ops.append(("DELETE FROM scraped_articles WHERE id = ?", (aid,)))
        summary["deletes"] += 1

    # Rule 2: Manifest has ID, SQLite doesn't -> INSERT into SQLite
    for aid in manifest_ids - sqlite_ids:
        loaded = store.load_article_for_reconciliation(aid)
        if loaded is None:
            # Self-Healing: Purge ghost entry
            del manifest["articles"][aid]
            ghosts_purged = True
            summary["errors"] += 1
            continue

        meta, emb = loaded
        vec_rowid = meta.get("vec_rowid")
        if vec_rowid is not None:
            vec_rowid = int(vec_rowid)
        embedding_status = meta.get("embedding_status", "PENDING")

        insert_sql = """
            INSERT OR REPLACE INTO scraped_articles (
                id, vec_rowid, url, title, conclusion, summary,
                metadata_json, embedding_status, scraped_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        safe_updated_at = meta.get("updated_at", datetime.now(timezone.utc).isoformat())
        ops.append((
            insert_sql,
            (
                aid, vec_rowid, meta.get("url", ""),
                meta.get("title"), meta.get("conclusion"), meta.get("summary"),
                meta.get("metadata_json", "{}"), embedding_status,
                safe_updated_at,
                safe_updated_at,
            ),
        ))

        if emb and embedding_status == "EMBEDDED" and vec_rowid is not None:
            ops.append(("DELETE FROM scraped_articles_vec WHERE rowid = ?", (vec_rowid,)))
            ops.append(("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, emb)))
        
        summary["inserts"] += 1

    # Rule 3: Both exist, manifest newer -> UPDATE SQLite
    for aid in manifest_ids & sqlite_ids:
        manifest_updated = manifest["articles"][aid].get("updated_at", "")
        sqlite_updated = sqlite_dict.get(aid, "")
        if manifest_updated > sqlite_updated:
            loaded = store.load_article_for_reconciliation(aid)
            if loaded is None:
                # Self-Healing: Purge ghost entry
                del manifest["articles"][aid]
                ghosts_purged = True
                summary["errors"] += 1
                continue

            meta, emb = loaded
            vec_rowid = meta.get("vec_rowid")
            if vec_rowid is not None:
                vec_rowid = int(vec_rowid)
            embedding_status = meta.get("embedding_status", "PENDING")

            update_sql = """
                UPDATE scraped_articles SET
                    vec_rowid = ?, url = ?, title = ?, conclusion = ?,
                    summary = ?, metadata_json = ?, embedding_status = ?,
                    updated_at = ?
                WHERE id = ?
            """
            safe_updated_at = meta.get("updated_at", datetime.now(timezone.utc).isoformat())
            ops.append((
                update_sql,
                (
                    vec_rowid, meta.get("url", ""), meta.get("title"),
                    meta.get("conclusion"), meta.get("summary"),
                    meta.get("metadata_json", "{}"), embedding_status,
                    safe_updated_at, aid,
                ),
            ))

            if emb and embedding_status == "EMBEDDED" and vec_rowid is not None:
                ops.append(("DELETE FROM scraped_articles_vec WHERE rowid = ?", (vec_rowid,)))
                ops.append(("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (vec_rowid, emb)))

            summary["updates"] += 1

    # Save manifest if ghosts were purged
    if ghosts_purged:
        store._save_manifest()
        log.dual_log(tag="Article:Reconcile:GhostPurge", level="INFO", message="Purged ghost entries from manifest", payload={"purged": True})

    # Chunked Execution
    CHUNK_SIZE = 1000
    for i in range(0, len(ops), CHUNK_SIZE):
        store.enqueue_tx(ops[i:i + CHUNK_SIZE])

    store.mark_synced()

    log.dual_log(
        tag="Article:Reconcile:Complete",
        level="INFO",
        message="Delta reconciliation complete",
        payload=summary,
    )

    return summary
