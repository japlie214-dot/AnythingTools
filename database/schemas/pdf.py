# database/schemas/pdf.py

TABLES = {
    "pdf_parsed_pages": """CREATE TABLE pdf_parsed_pages (
            id INTEGER NOT NULL PRIMARY KEY,
            chat_id INTEGER,
            pdf_name TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            content TEXT,
            embedding_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(embedding_status IN ('PENDING','EMBEDDED','SKIPPED')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE INDEX idx_pdf_pages_file_page ON pdf_parsed_pages(pdf_name, page_number);
""",
}

VEC_TABLES = {
    "pdf_parsed_pages_vec": """CREATE VIRTUAL TABLE IF NOT EXISTS pdf_parsed_pages_vec USING vec0(embedding float[1024]);
""",
}
