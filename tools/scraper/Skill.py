# Skill.py
"""
Tool descriptor for the scraper tool.
The registry imports `desc` from this module when present and uses it as the
human-readable description exposed in the MCP manifest.
"""

# Short description string used by the registry as the tool description
desc = """
Scraper Skill (AnythingTools)

Goal
- Discover and extract article metadata and short conclusions from a specified target news site. Produce a curated Top-10 list (by internal ULID) and persist both raw scrape output and curated JSON into the artifacts root.

Arguments (INPUT_MODEL)
- target_site: string — one of the supported site identifiers (e.g., "FT", "Bloomberg").

Output Data
- `data` (when run via the API worker) will include a human-readable execution_report string in the `result` field.
- Tools MUST also produce artifact paths under `artifacts/scrapes/`.

Artifacts
- `artifacts/scrapes/scraper_output_<ts>.json` — raw JSON output of per-URL extraction
- `artifacts/scrapes/top_10_<batch_id>.json` — curated Top 10 list (JSON array of article objects)

Notes
- The tool is cooperative-cancelable: it accepts a `cancellation_flag` and will stop when it is set.
- Prompts used by sub-agents require strict JSON-only responses (see `prompt.py`).
"""
