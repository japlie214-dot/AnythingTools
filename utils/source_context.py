# utils/source_context.py
"""Precision source extraction for Logger Agent diagnostics.

!! MANDATORY MAINTENANCE NOTICE !!
This module's MAPPING dictionary MUST be updated whenever:
  - A new tool is added to tools/
  - An existing tool's critical execution functions are renamed or relocated

The Logger Agent depends on these mappings to retrieve source code for
diagnostic analysis. Unmapped tools receive degraded diagnoses based on
traceback and logs alone.
"""

import inspect
import importlib
import sys


class SourceContextManager:
    """Resolves tool step keys to raw source code strings."""

    # Composite key → (module_dotted_path, dotted_attribute_chain)
    MAPPING: dict[str, tuple[str, str]] = {
        # ── Finance ──
        "finance:ingest":    ("tools.finance.ingestion", "ingest_sec_fundamentals"),
        "finance:pipeline":  ("tools.finance.pipeline",  "run_financial_pipeline"),
        "finance:validate":  ("tools.finance.tool",      "FinanceTool._handle_analyze"),
        # ── Research ──
        "research:scrape":   ("tools.research.scraper_agent", "AgenticBrowserScraper.scrape"),
        "research:pdf":      ("tools.research.pdf_engine",    "ReportEngine.generate"),
        "research:run":      ("tools.research.tool",          "ResearchTool.run"),
        # ── Search ──
        "search:fetch":      ("tools.search", "SearchTool.run"),
        "search:synthesis":  ("tools.search", "SearchTool.run"),
        # ── Quiz ──
        "quiz:sanitize":     ("tools.quiz.tool", "sanitize_quiz_question"),
        "quiz:translate":    ("tools.quiz.tool", "QuizTool.run"),
        "quiz:deliver":      ("tools.quiz.tool", "QuizTool._deliver_polls"),
        # ── Scraper ──
        "scraper:run":       ("tools.scraper.tool", "ScraperTool.run"),
        "scraper:scrape":    ("tools.scraper.tool", "_run_botasaurus_scraper"),
    }

    @classmethod
    def get_source(cls, step_key: str) -> str:
        """Return the raw source string for *step_key*, or a placeholder on failure."""
        if step_key not in cls.MAPPING:
            return f"<source unavailable for {step_key}: unmapped key>"

        module_path, attr_chain = cls.MAPPING[step_key]
        try:
            module = importlib.import_module(module_path)
            obj = module
            for part in attr_chain.split("."):
                obj = getattr(obj, part)

            while hasattr(obj, "__wrapped__"):
                obj = obj.__wrapped__

            return inspect.getsource(obj)
        except Exception as exc:
            sys.stderr.write(f"Warning: source extraction failed for {step_key}: {exc}\n")
            return f"<source unavailable for {step_key}: {exc}>"

    @classmethod
    def get_tool_sources(cls, tool_name: str) -> str:
        """Retrieve concatenated source for all MAPPING entries matching ``tool_name:`` prefix.

        Returns a descriptive placeholder if no keys match the given tool name.
        """
        prefix = f"{tool_name}:"
        matching_keys = sorted(k for k in cls.MAPPING if k.startswith(prefix))

        if not matching_keys:
            return f"<No source mappings found for tool '{tool_name}'>"

        parts: list[str] = []
        for key in matching_keys:
            parts.append(f"\n--- SOURCE: {key} ---\n")
            parts.append(cls.get_source(key))
        return "".join(parts)
    