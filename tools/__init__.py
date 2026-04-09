# tool/__init__.py
"""Tools package.

ARCHITECTURE DEFINITION:
1. Public Tools (tools/<name>/): High-level workflows exposed via the FastAPI
   `/tools/{name}` endpoints (e.g., research, finance, scraper, library_query).
   These are the entry points for the external Caller Agent.
2. Agent Actions (tools/actions/<scope>/): Granular, internal capabilities used
   exclusively by sub-agents (e.g., `browser:click`, `system:read_file`).
   These are strictly namespaced to prevent tool confusion and are invisible
   to the external API.

Public tool: library_query
- A public tool available to the API.
- Automatically discovered by Registry's legacy top-level scan.

Agent Actions (new canonical locations):
- tools/actions/system/files/        → system:file_list_downloads, etc.
- tools/actions/system/skills/       → system:skill_list, etc.
- tools/actions/system/drafteditor/  → system:draft_editor
- tools/actions/browser/browser_operator → browser:operator
- tools/actions/browser/macros/      → browser:macro_*
- tools/actions/library/pdf_search/  → library:pdf_search, library:get_pdf_toc
- tools/actions/library/vector_search.py  → library:vector_search

This package provides the tools namespace under which individual
tool packages live. It intentionally does NOT expose prompt templates
or constants because prompts belong inside each tool package or in
`utils/` for shared prompts. Keeping prompts out of `tools.__init__`
prevents accidental import of prompt-only modules when scanning the
tools package at import-time.
"""

__all__ = [
    "registry",  # dynamic discovery lives in tools.registry
]
