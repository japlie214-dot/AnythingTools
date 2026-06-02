# scripts/migrate_artifacts.py

import os
import json
import sqlite3
from pathlib import Path

def migrate():
    db_path = Path("data/sumanal.db")
    legacy_dir = Path("backups/articles")

    if not db_path.exists():
        print(f"[ERROR] Database {db_path} not found. Ensure you run this from the project root.")
        return

    if not legacy_dir.exists():
        print(f"[INFO] Legacy directory {legacy_dir} not found. Nothing to migrate.")
        return

    print(f"[INFO] Starting migration from {legacy_dir} into {db_path}...")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    success_count = 0
    fail_count = 0

    for json_file in legacy_dir.glob("*.json"):
        article_id = json_file.stem
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            
            bin_file = json_file.with_suffix(".bin")
            embedding_bytes = None
            if bin_file.exists():
                with open(bin_file, "rb") as f:
                    embedding_bytes = f.read()

            cursor.execute("""
                INSERT OR REPLACE INTO scraped_articles (
                    id, vec_rowid, url, title, conclusion, summary,
                    metadata_json, embedding_status, scraped_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (
                article_id,
                meta.get("vec_rowid"),
                meta.get("url", ""),
                meta.get("title", ""),
                meta.get("conclusion", ""),
                meta.get("summary", ""),
                meta.get("metadata_json", "{}"),
                meta.get("embedding_status", "PENDING")
            ))

            if embedding_bytes and meta.get("vec_rowid"):
                cursor.execute("DELETE FROM scraped_articles_vec WHERE rowid = ?", (meta["vec_rowid"],))
                cursor.execute("INSERT INTO scraped_articles_vec (rowid, embedding) VALUES (?, ?)", (meta["vec_rowid"], embedding_bytes))
                cursor.execute("UPDATE scraped_articles SET embedding_status = 'EMBEDDED' WHERE id = ?", (article_id,))

            success_count += 1
            print(f"[SUCCESS] Migrated artifact: {article_id}")
        except Exception as e:
            fail_count += 1
            print(f"[FAILED] Failed to migrate {article_id}: {e}")

    conn.commit()
    conn.close()
    
    print("\n========================================")
    print(f"Migration Complete. Success: {success_count}, Failed: {fail_count}")
    print("========================================")
    print("You may now safely delete the legacy 'backups/articles/' directory.")

if __name__ == "__main__":
    migrate()
