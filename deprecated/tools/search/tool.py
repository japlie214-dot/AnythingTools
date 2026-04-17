# deprecated/tools/search/tool.py
#
# SearchTool — lightweight web search via DuckDuckGo.
# Does NOT launch a browser. Uses the ddgs library to fetch
# text and news results, deduplicates by normalized URL, then synthesizes
# an answer via the Azure LLM provider.

import asyncio
import time
import re as _re
from typing import Any

from tools.base import BaseTool
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger
from utils.search_client import SearchClient

log = get_dual_logger(__name__)
from tools.search.search_prompts import SEARCH_INITIAL_PROMPT, SEARCH_EVALUATION_VERBOSE_PROMPT, SEARCH_FINAL_SYNTHESIS_PROMPT
import config


class SearchTool(BaseTool):
    name = "search"

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """
        Iterative, evaluate-and-refine DuckDuckGo search loop with LLM synthesis.

        Args:
            args: Must contain "query" (str).
            telemetry: Async callback for emitting status updates.

        Returns:
            Synthesized answer string, or an error message.
        """
        initial_query = args.get('query', '')
        if not initial_query:
            return 'Error: No search query provided.'

        await telemetry(self.status(f"Routing query to SearchTool: '{initial_query}'", "ROUTING"))

        llm = get_llm_client(provider_type='azure')
        search_history: list[str] = []
        current_query = initial_query
        seen_urls: set[str] = set()

        def normalize(url: str) -> str:
            """Strip query string and trailing slash for dedup."""
            return url.split("?")[0].strip("/")

        def fetch_ddg(q: str) -> list[str]:
            """Blocking call — runs in a thread via asyncio.to_thread."""
            # Use the centralized SearchClient
            final_res = SearchClient.fetch_text_and_news(q, max_text=5, max_news=3)
            
            # Deduplicate against seen_urls
            deduplicated = []
            for r in final_res:
                # Extract URL from the result format
                lines = r.split('\n')
                url_line = next((line for line in lines if line.startswith('URL: ')), None)
                if url_line:
                    url = url_line[5:]  # Remove 'URL: ' prefix
                    if normalize(url) not in seen_urls:
                        seen_urls.add(normalize(url))
                        deduplicated.append(r)
            
            log.dual_log(
                tag="Tool:Search:Fetch",
                message=f"DDG fetch complete for query: {q!r}   {len(deduplicated)} result(s)",
                payload={"results": deduplicated},
            )
            return deduplicated

        def clean_query(q: str) -> str:
            """Remove conversational noise and limit query length."""
            # Remove "Search for", "Look up", "Query:", "Find info about", etc.
            q = _re.sub(r'^(search for|look up|query|find|please find|refinement)\b:?', '', q, flags=_re.I).strip()
            # Truncate to first 150 characters to prevent 400 errors
            return q[:150]

        try:
            for loop in range(1, config.SEARCH_MAX_LOOPS + 1):
                await telemetry(self.status(
                    f'[{loop}/{config.SEARCH_MAX_LOOPS}] Searching: {current_query[:60]}',
                    'RUNNING',
                ))

                sanitized_query = clean_query(current_query)
                results = await asyncio.to_thread(fetch_ddg, sanitized_query)
                if not results:
                    log.dual_log(tag="Tool:Search", message=f"No results found for: {sanitized_query}", level="WARNING")
                    # If first loop yields nothing, abort synthesis and fail
                    if loop == 1:
                        return f"Error: No search results returned for query: {sanitized_query}"
                    break
                context = '\n\n'.join(results)

                # Map step — synthesise partial answer
                from utils.text_processing import escape_prompt_separators
                synthesis = await llm.complete_chat(LLMRequest(
                    messages=[{'role': 'user', 'content': SEARCH_INITIAL_PROMPT.format(
                        current_query=escape_prompt_separators(current_query),
                        context=escape_prompt_separators(context)
                    )}]
                ))
                search_history.append(
                    f'Q: {current_query}\nA: {synthesis.content}'
                )

                if loop == config.SEARCH_MAX_LOOPS:
                    break

                # Evaluate step — ask if more evidence is needed
                from utils.text_processing import escape_prompt_separators
                eval_resp = await llm.complete_chat(LLMRequest(
                    messages=[{'role': 'user', 'content': SEARCH_EVALUATION_VERBOSE_PROMPT.format(
                        initial_query=escape_prompt_separators(initial_query),
                        search_history=escape_prompt_separators(chr(10).join(search_history))
                    )}]
                ))

                if '<satisfied>Yes</satisfied>' in eval_resp.content:
                    break
                m = _re.search(r'<refinement>(.*?)</refinement>',
                               eval_resp.content, _re.DOTALL)
                if m:
                    current_query = m.group(1).strip()
                else:
                    break

            # Fail early if no evidence was ever retrieved
            if not search_history:
                return f"Error: SearchTool failed to retrieve any evidence for '{initial_query}' after multiple attempts."

            # Reduce step — final synthesis over all gathered evidence
            from utils.text_processing import escape_prompt_separators
            final = await llm.complete_chat(LLMRequest(
                messages=[{'role': 'user', 'content': SEARCH_FINAL_SYNTHESIS_PROMPT.format(
                    initial_query=escape_prompt_separators(initial_query),
                    search_history=escape_prompt_separators(chr(10).join(search_history))
                )}]
            ))
            await telemetry(self.status('Search complete.', 'SUCCESS'))
            return final.content

        except Exception as e:
            error_msg = f"Search execution failed: {e}"
            log.dual_log(
                tag="Tool:Search",
                message=error_msg,
                level="ERROR",
                exc_info=True,
            )
            await telemetry(self.status("Search API failed.", "ERROR"))
            return error_msg
