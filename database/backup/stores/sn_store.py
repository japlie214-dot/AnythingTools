# database/backup/stores/sn_store.py
import json
from typing import List, Tuple, Optional
from database.backup.base_store import JsonStore
from database.writer import enqueue_write

class SnFilingStore(JsonStore):
    entity_key = "filing_id"
    manifest_entity_key = "sn_filings"

    def build_upsert_statements(self, entity_id: str, data: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]:
        sql = """INSERT OR REPLACE INTO sn_filings 
                 (filing_id, ticker, form, filing_date, accession_no, period_of_report, company_name, cik, fiscal_year_end_month, quarter, year, updated_at) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        return [(sql, (entity_id, data.get("ticker"), data.get("form"), data.get("filing_date"), data.get("accession_no"), data.get("period_of_report"), data.get("company_name"), data.get("cik"), data.get("fiscal_year_end_month"), data.get("quarter"), data.get("year"), data.get("updated_at")))]

    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]:
        return [("DELETE FROM sn_filings WHERE filing_id = ?", (entity_id,))]

    def get_all_from_sqlite(self, conn) -> List[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM sn_filings").fetchall()]

    def reconcile(self, conn) -> dict:
        base_summary = super().reconcile(conn)
        self.rebuild_dynamic_tables_offline()
        return base_summary

    def rebuild_dynamic_tables_offline(self):
        """Reads all JSON payloads from disk and rebuilds sn_dt_* tables dynamically."""
        for file_path in self.backup_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                    for note in payload.get("notes", []):
                        for table in note.get("tables", []):
                            dt_name = table.get("name")
                            rows_data = table.get("data", [])
                            if not dt_name or not rows_data:
                                continue
                            
                            columns = list(rows_data[0].keys())
                            col_defs = ["_row_id INTEGER PRIMARY KEY AUTOINCREMENT"]
                            for col in columns:
                                safe_col = col.replace('"', '')
                                col_defs.append(f'"{safe_col}" TEXT')
                            
                            ddl = f'CREATE TABLE IF NOT EXISTS "{dt_name}" ({", ".join(col_defs)})'
                            enqueue_write(ddl)
                            
                            for row in rows_data:
                                cols = [f'"{c.replace(chr(34), "")}"' for c in columns]
                                vals = [str(row.get(c, "")) for c in columns]
                                placeholders = ", ".join(["?"] * len(cols))
                                sql = f'INSERT OR REPLACE INTO "{dt_name}" ({", ".join(cols)}) VALUES ({placeholders})'
                                enqueue_write(sql, tuple(vals))
            except Exception as e:
                from utils.logger import get_dual_logger
                get_dual_logger(__name__).dual_log(tag="Store:SnFiling:RebuildError", message=f"Failed to rebuild dynamic table offline: {e}", level="ERROR", payload={"file": str(file_path), "error": str(e)})

class SnNoteStore(JsonStore):
    entity_key = "note_id"
    manifest_entity_key = "sn_notes"

    def build_upsert_statements(self, entity_id: str, data: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]:
        sql = """INSERT OR REPLACE INTO sn_notes 
                 (note_id, filing_id, ticker, form, accession_no, note_number, title, short_name, narrative_text, narrative_hash, expands, expands_statements, table_count, details_count, quarter, year, quarterly_status, version, updated_at) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        return [(sql, (
            entity_id, data.get("filing_id"), data.get("ticker"), data.get("form"),
            data.get("accession_no"), data.get("note_number"), data.get("title", ""),
            data.get("short_name", ""), data.get("narrative_text", ""), data.get("narrative_hash", ""),
            data.get("expands", "[]"), data.get("expands_statements", "[]"), data.get("table_count", 0),
            data.get("details_count", 0), data.get("quarter", 0), data.get("year", 0),
            data.get("quarterly_status", ""), data.get("version", 1), data.get("updated_at")
        ))]

    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]:
        return [("DELETE FROM sn_notes WHERE note_id = ?", (entity_id,))]

    def get_all_from_sqlite(self, conn) -> List[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM sn_notes").fetchall()]

class SnDetailRegistryStore(JsonStore):
    entity_key = "registry_id"
    manifest_entity_key = "sn_detail_registry"

    def build_upsert_statements(self, entity_id: str, data: dict, embedding_bytes: Optional[bytes] = None) -> List[Tuple[str, tuple]]:
        sql = """INSERT OR REPLACE INTO sn_detail_registry 
                 (registry_id, ticker, detail_table_name, source_title, source_note_number, source_accession_no, role_or_type, column_schema, row_count, quarter, year, quarterly_status, updated_at) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        return [(sql, (
            entity_id, data.get("ticker"), data.get("detail_table_name"), data.get("source_title", ""),
            data.get("source_note_number", 0), data.get("source_accession_no", ""), data.get("role_or_type", ""),
            data.get("column_schema", "[]"), data.get("row_count", 0), data.get("quarter", 0), data.get("year", 0),
            data.get("quarterly_status", ""), data.get("updated_at")
        ))]

    def build_delete_statements(self, entity_id: str) -> List[Tuple[str, tuple]]:
        return [("DELETE FROM sn_detail_registry WHERE registry_id = ?", (entity_id,))]

    def get_all_from_sqlite(self, conn) -> List[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM sn_detail_registry").fetchall()]
