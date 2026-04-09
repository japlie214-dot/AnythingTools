# tools/actions/library/pdf_search/tool.py
import sqlite3
from typing import Any
import config
from tools.base import BaseTool, TelemetryCallback
from database.connection import DatabaseManager
from utils.vector_search import generate_embedding

class PDFSearchTool(BaseTool):
    name = "library:pdf_search"

    async def run(self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs) -> str:
        pdf_name = args.get("pdf_name")
        search_query = args.get("search_query")
        page_range = args.get("page_range")
        
        if not pdf_name: return "Error: pdf_name required."
        if not search_query and not page_range: return "Error: search_query or page_range required."
        
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        results = []
        
        # Parse page ranges
        allowed_pages = set()
        if page_range:
            for part in page_range.split(","):
                if "-" in part:
                    try:
                        s, e = part.split("-")
                        allowed_pages.update(range(int(s), int(e)+1))
                    except ValueError: pass
                else:
                    try: allowed_pages.add(int(part))
                    except ValueError: pass

        if search_query:
            emb = await generate_embedding(search_query)
            # Vector Search
            sql = """
                SELECT p.page_number, p.content, (1 - v.distance) as sim
                FROM pdf_parsed_pages_vec v
                JOIN pdf_parsed_pages p ON v.rowid = CAST(p.id AS INTEGER)
                WHERE v.embedding MATCH ? AND k = 50 AND p.pdf_name = ?
            """
            rows = conn.execute(sql, (emb, pdf_name)).fetchall()
            
            # Filter by range if provided
            if allowed_pages:
                rows = [r for r in rows if r["page_number"] in allowed_pages]
            
            results = [r for r in rows if r["sim"] > 0.4]
            results.sort(key=lambda x: x["sim"], reverse=True)
        else:
            # Range-only
            if not allowed_pages: return "Invalid page range format."
            pl = ",".join("?" for _ in allowed_pages)
            sql = f"SELECT page_number, content FROM pdf_parsed_pages WHERE pdf_name = ? AND page_number IN ({pl})"
            rows = conn.execute(sql, [pdf_name] + list(allowed_pages)).fetchall()
            results = [{"page_number": r["page_number"], "content": r["content"], "sim": 1.0} for r in rows]
            results.sort(key=lambda x: x["page_number"]) 
        
        if not results:
            return "No matching pages found."
        
        # Budget Guillotine
        budget = int(config.LLM_CONTEXT_CHAR_LIMIT * config.BUDGET_FRAC_ATTACHMENT)
        output_parts = []
        total_len = 0
        omitted = []
        
        for r in results:
            part = f"--- PAGE {r['page_number']} (Sim: {r['sim']:.2f}) ---\n{r['content']}\n\n"
            if total_len + len(part) > budget:
                omitted.append(r['page_number'])
            else:
                output_parts.append(part)
                total_len += len(part)
        
        header = ""
        if omitted:
            header = f"⚠️ **Context Budget Reached**: Lower-relevance pages omitted: {omitted}\n\n"
        
        return header + "".join(output_parts)
