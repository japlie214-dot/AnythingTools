# tools/search/tool.py
#
# SearchTool — lightweight web search via DuckDuckGo.
# Does NOT launch a browser. Uses the ddgs library to fetch
# text and news results, deduplicates by normalized URL, then synthesizes
# an answer via the Azure LLM provider.

import asyncio
import time
import re as _re
from typing import Any

from ddgs import DDGS

from tools.base import BaseTool
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
from tools.search.search_prompts import SEARCH_INITIAL_PROMPT, SEARCH_EVALUATION_VERBOSE_PROMPT, SEARCH_FINAL_SYNTHESIS_PROMPT
import config

# Maximum number of retry attempts for transient DDGS failures
_DDGS_MAX_RETRIES = 2
_DDGS_RETRY_DELAY_S = 3


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
            text_res: list[str] = []
            news_res: list[str] = []

            for attempt in range(_DDGS_MAX_RETRIES + 1):
                try:
                    with DDGS() as ddgs:
                        for r in ddgs.text(q, region="wt-wt", safesearch="moderate", max_results=5):
                            url = r.get("href")
                            if url and normalize(url) not in seen_urls:
                                seen_urls.add(normalize(url))
                                text_res.append(
                                    f"Title: {r.get('title')}\n"
                                    f"URL: {url}\n"
                                    f"Snippet: {r.get('body')}"
                                )

                        for r in ddgs.news(q, region="wt-wt", safesearch="moderate", max_results=3):
                            url = r.get("url", r.get("href"))
                            if url and normalize(url) not in seen_urls:
                                seen_urls.add(normalize(url))
                                news_res.append(
                                    f"Title: {r.get('title')}\n"
                                    f"URL: {url}\n"
                                    f"Snippet: {r.get('body')}"
                                )
                    break  # success
                except Exception as exc:
                    if attempt < _DDGS_MAX_RETRIES:
                        log.dual_log(
                            tag="Tool:Search",
                            message=f"DDGS attempt {attempt + 1} failed, retrying in {_DDGS_RETRY_DELAY_S}s: {exc}",
                            level="WARNING",
                        )
                        time.sleep(_DDGS_RETRY_DELAY_S)
                    else:
                        log.dual_log(
                            tag="Tool:Search",
                            message=f"DDGS fetch exhausted retries for query: {q}",
                            level="ERROR",
                        )
            final_res = text_res + news_res
            log.dual_log(
                tag="Tool:Search:Fetch",
                message=f"DDG fetch complete for query: {q!r}  {len(final_res)} result(s)",
                payload={"results": final_res},
            )
            return final_res

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
