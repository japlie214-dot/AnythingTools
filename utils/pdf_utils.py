# utils/pdf_utils.py
import asyncio
import re
import os
from pypdf import PdfReader
from database.writer import enqueue_write
from utils.vector_search import generate_embedding
from utils.id_generator import ULID
import config

async def process_pdf(file_path: str, file_name: str, chat_id: int):
    """Extracts text, generates embeddings, and stores in DB."""
    try:
        reader = PdfReader(file_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            pages.append((i + 1, text.strip()))
    except Exception:
        return None, None
    
    total_text = "".join(p[1] for p in pages)
    if not total_text.strip():
        return None, None
    
    # ToC Detection
    toc_pages = []
    for num, text in pages:
        lower = text.lower()
        if any(kw in lower for kw in ["contents", "index", "table of contents", "daftar isi"]):
            toc_pages.append(num)
        elif len(re.findall(r'\.{3,}\s*\d+', text)) > 3:
            toc_pages.append(num)
    
    toc_meta = "No ToC detected."
    if toc_pages:
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
        if blocks:
            longest = max(blocks, key=len)
            toc_meta = f"Detected ToC on pages: {longest[0]}-{longest[-1]}"

    # DB Insertion with Concurrency Control
    sem = asyncio.Semaphore(15)
    async def process_page(num, text):
        if not text.strip(): return
        async with sem:
            try:
                emb = await generate_embedding(text)
                pid = ULID.generate()
                vec_rowid = abs(hash(pid)) % (2**63)
                # Store text in main table
                enqueue_write(
                    "INSERT INTO pdf_parsed_pages (id, chat_id, pdf_name, page_number, content) VALUES (?, ?, ?, ?, ?)",
                    (str(vec_rowid), chat_id, file_name, num, text)
                )
                # Store vector in virtual table
                enqueue_write(
                    "INSERT INTO pdf_parsed_pages_vec (rowid, embedding) VALUES (?, ?)",
                    (vec_rowid, emb)
                )
            except Exception:
                pass
            
    await asyncio.gather(*(process_page(num, text) for num, text in pages))
    
    full_text = f"--- PDF METADATA ---\nName: {file_name}\nPages: {len(pages)}\n{toc_meta}\n\n" + "\n\n".join(f"--- PAGE {p[0]} ---\n{p[1]}" for p in pages)
    return full_text, toc_meta
