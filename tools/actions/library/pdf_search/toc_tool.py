# tools/actions/library/pdf_search/toc_tool.py
import sqlite3
import re
from typing import Any
from tools.base import BaseTool
from database.connection import DatabaseManager

class PDFTocTool(BaseTool):
    from bot.core.constants import TOOL_LIBRARY_GET_PDF_TOC
    name = TOOL_LIBRARY_GET_PDF_TOC

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        pdf_name = args.get("pdf_name")
        if not pdf_name: return "Error: pdf_name required."
        
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT page_number, content FROM pdf_parsed_pages WHERE pdf_name = ? ORDER BY page_number", (pdf_name,)).fetchall()
        
        if not rows: return f"No pages found for PDF '{pdf_name}'."
        
        # Re-run Heuristic
        toc_pages = []
        for r in rows:
            lower = r["content"].lower()
            if any(kw in lower for kw in ["contents", "index", "table of contents", "daftar isi"]):
                toc_pages.append(r["page_number"]) 
            elif len(re.findall(r'\.{3,}\s*\d+', r["content"])) > 3:
                toc_pages.append(r["page_number"]) 
        
        if toc_pages:
            # Find longest contiguous block
            toc_pages.sort()
            blocks = []
            current = [toc_pages[0]]
            for p in toc_pages[1:]:
                if p == current[-1] + 1:
                    current.append(p)
                else:
                    blocks.append(current)
                    current = [p]
            blocks.append(current)
            longest = max(blocks, key=len)
            
            pl = ",".join("?" for _ in longest)
            tocs = conn.execute(f"SELECT page_number, content FROM pdf_parsed_pages WHERE pdf_name = ? AND page_number IN ({pl})", [pdf_name] + longest).fetchall()
            return "\n\n".join(f"--- TOC PAGE {t['page_number']} ---\n{t['content']}" for t in tocs)
        else:
            return "No ToC detected in this document."
