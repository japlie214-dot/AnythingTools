# deprecated/tools/library_query.py
"""Library Query Tool – Librarian sub-agent entry point.

Public tool that accepts a query string and hands it off to the internal
`library:vector_search` agent action. The Librarian synthesizes findings
and enforces strict knowledge boundaries (no internal knowledge).
"""

import json
from typing import Any

from tools.base import BaseTool
from clients.llm import get_llm_client, LLMRequest


class LibraryQueryTool(BaseTool):
    """Public tool that serves as the Librarian sub-agent entry point."""

    name = "library_query"

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        query = args.get("query", "")
        if not query:
            return "Error: query required."

        await telemetry(self.status("Librarian sub-agent activated.", "RUNNING"))

        # Import lazily to avoid circular dependencies.
        from tools.actions.library.vector_search import VectorSearchAction

        search_action = VectorSearchAction()
        llm = get_llm_client("azure")
        search_history = []

        for attempt in range(1, 4):
            await telemetry(self.status(f"Searching library (Attempt {attempt}/3)...", "RUNNING"))

            # Recursive refinement could be injected here by asking the LLM to
            # yield a new query. For simplicity, we execute the exact query first.
            results = await search_action.run({"query": query, "limit": 5}, telemetry, **kwargs)
            parsed_results = json.loads(results)

            if parsed_results.get("count", 0) > 0:
                search_history.append(
                    f"Found relevant articles:\n{json.dumps(parsed_results.get('data'), indent=2)}"
                )
                break
            else:
                search_history.append(f"Attempt {attempt}: No relevant articles found for '{query}'.")

        if not any("Found relevant articles" in h for h in search_history):
            return "No data. I have searched the internal library, but no relevant articles exist."

        prompt = (
            "You are a strict Librarian. Synthesize the findings below to answer the user's query.\n"
            "RULE: Do NOT use internal knowledge. If the answer is not in the data, respond exactly with 'No data'.\n\n"
            f"Query: {query}\n\nData:\n" + "\n".join(search_history)
        )

        res = await llm.complete_chat(LLMRequest(messages=[{"role": "user", "content": prompt}]))
        return res.content
