# tools/scraper/__init__.py
"""
Scraper Tool Documentation
==========================

Use this tool to scrape and curate top articles from a target site. The scraper
extracts article metadata, curates a Top 10 list using LLM-based impact scoring,
and creates a broadcast batch for subsequent publishing.

Endpoint: /api/tools/scraper

Filling Instructions:

${target_site}: The target news site to scrape. Must be one of the supported
    target sites in VALID_TARGET_NAMES. Examples: "reuters", "apnews", "bbc"

Schema:
{
  "type": "object",
  "properties": {
    "target_site": {
      "type": "string",
      "description": "REQUIRED: The target news site to scrape."
    }
  },
  "required": ["target_site"]
}

Developer Notes:
----------------
- Resume Mechanism: This tool supports partial resumption via /api/jobs/${job_id}/resume. 
  When a scraping job is interrupted, the system can resume from the point of the last
  completed article extraction. The scraper tracks progress natively via the job_items table.
- Browser Lock: Scraper executions are guarded by a singleton `browser_lock`.
- SoM Targeting: Set of Marks targeting state is not fully serialized, so a resumed job 
  might extract links using standard locators if SoM memory is lost.
"""

from .tool import ScraperTool

__all__ = ["ScraperTool"]
