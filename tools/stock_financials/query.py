# tools/stock_financials/query.py
import sqlite3
import logging
from typing import List, Dict, Any, Optional
from database.connection import DatabaseManager

def query_facts(
    ticker: str,
    statement_type: str,
    concept: Optional[str] = None,
    start_quarter: Optional[str] = None,
    end_quarter: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    conn = DatabaseManager.get_read_connection()
    conn.row_factory = sqlite3.Row
    where = ["ticker = ?", "statement_type = ?"]
    params: List[Any] = [ticker.upper(), statement_type.lower()]
    if concept:
        # Concepts are stored in raw SEC EDGAR XBRL form (e.g. "us-gaap:Assets").
        # Backward-compat: if a caller passes the legacy normalized form
        # ("us-gaap_Assets"), restore the colon and emit a deprecation warning.
        normalized = concept.replace("_", ":", 1) if concept.startswith("us-gaap_") else concept
        if normalized != concept:
            logging.getLogger(__name__).warning("StockFin:Concept:LegacyFormat — received '%s', normalized to '%s'. Update caller to use the raw XBRL format.", concept, normalized)
        where.append("concept = ?")
        params.append(normalized)
    if start_quarter:
        where.append("quarter >= ?")
        params.append(start_quarter)
    if end_quarter:
        where.append("quarter <= ?")
        params.append(end_quarter)
    sql = f"SELECT * FROM sf_quarterly_facts WHERE {' AND '.join(where)} ORDER BY concept_order ASC, quarter DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def query_concepts(ticker: str, statement_type: Optional[str] = None) -> List[str]:
    conn = DatabaseManager.get_read_connection()
    where = ["ticker = ?"]
    params = [ticker.upper()]
    if statement_type:
        where.append("statement_type = ?")
        params.append(statement_type.lower())
    sql = f"SELECT DISTINCT concept FROM sf_quarterly_facts WHERE {' AND '.join(where)} ORDER BY concept"
    return [r[0] for r in conn.execute(sql, params).fetchall()]
