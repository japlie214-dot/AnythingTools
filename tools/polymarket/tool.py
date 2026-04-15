# tools/polymarket/tool.py
"""Polymarket tool — canonical package-local implementation.

This module contains the PolymarketTool class migrated from the former
`tools/polymarket.py` script into the canonical package layout and fixes the
partial-write issue observed earlier.
"""

import asyncio
import httpx
import json
import re
from typing import Any

from ddgs import DDGS
import config
from tools.base import BaseTool
from clients.llm import get_llm_client, LLMRequest
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
from tools.polymarket.polymarket_prompts import (
    POLYMARKET_EVALUATION_PROMPT,
    POLYMARKET_SYNTHESIS_PROMPT,
)


class PolymarketTool(BaseTool):
    name = "polymarket"

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        topic = args.get("topic", "").lower()
        limit = int(args.get("limit", 1))

        await telemetry(self.status("Fetching trending events from Polymarket API...", "RUNNING"))
        events = await self._fetch_gamma_events(topic)

        if not events:
            return "No active events found on Polymarket at this time."

        top_events = events[:limit]
        reports: list[str] = []
        llm = get_llm_client(provider_type="azure")

        for i, event in enumerate(top_events, 1):
            title = event.get("title", "Unknown")
            context = event.get("probability_context", "")
            await telemetry(self.status(f"[{i}/{len(top_events)}] Researching: {title[:50]}", "RUNNING"))

            search_history = []
            current_query = title

            # ITERATIVE EVIDENCE EVALUATION LOOP
            for loop in range(getattr(config, 'POLYMARKET_MAX_RESEARCH_LOOPS', 3)):
                search_results = await asyncio.to_thread(self._web_search, current_query)
                if not search_results:
                    break

                search_history.append(f"Q: {current_query}\nA: {search_results}")

                # Evaluate if evidence is sufficient
                from utils.text_processing import escape_prompt_separators

                eval_prompt = POLYMARKET_EVALUATION_PROMPT.format(
                    title=escape_prompt_separators(title),
                    context=escape_prompt_separators(context),
                    search_history=escape_prompt_separators(chr(10).join(search_history)),
                )

                eval_resp = await llm.complete_chat(LLMRequest(messages=[{"role": "user", "content": eval_prompt}]))

                if "<satisfied>Yes</satisfied>" in getattr(eval_resp, 'content', ''):
                    break

                match = re.search(r"<refinement>(.*?)</refinement>", getattr(eval_resp, 'content', ''), re.DOTALL)
                if match:
                    current_query = match.group(1).strip()
                else:
                    break

            # STRUCTURED SYNTHESIS
            await telemetry(self.status(f"Synthesizing final report for {title[:30]}...", "RUNNING"))
            from utils.text_processing import escape_prompt_separators

            synthesis_prompt = POLYMARKET_SYNTHESIS_PROMPT.format(
                title=escape_prompt_separators(title),
                context=escape_prompt_separators(context),
                evidence=escape_prompt_separators(chr(10).join(search_history)),
            )

            try:
                response = await llm.complete_chat(LLMRequest(messages=[{"role": "user", "content": synthesis_prompt}]))
                reports.append(getattr(response, 'content', str(response)))
            except Exception as exc:
                reports.append(f"### 🎲 {title}\n_Analysis unavailable: {exc}_")

        await telemetry(self.status("Polymarket analysis complete.", "SUCCESS"))
        return "\n\n---\n\n".join(reports)

    # Gamma API with exponential back-off
    async def _fetch_gamma_events(self, topic: str) -> list[dict]:
        url = "https://gamma-api.polymarket.com/events?closed=false&active=true"
        backoff = 2.0
        for attempt in range(4):  # 1 initial + 3 retries
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                    resp.raise_for_status()
                    data = resp.json()
                break
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                if attempt == 3:
                    log.dual_log(tag="Polymarket", message=f"Gamma API exhausted retries: {exc}", level="ERROR")
                    return []
                log.dual_log(tag="Polymarket", message=f"Gamma API attempt {attempt + 1} failed, retrying in {backoff:.1f}s", level="WARNING")
                await asyncio.sleep(backoff)
                backoff *= 2
        else:
            return []

        parsed: list[dict] = []
        for event in data:
            tags_labels = [t.get('label', '').lower() for t in event.get('tags', [])]
            title_lower = event.get('title', '').lower()

            # Category-tag filter (requires at least one matching tag)
            if getattr(config, 'POLYMARKET_CATEGORY_TAGS', None):
                if not any(cat in tags_labels or cat in title_lower for cat in config.POLYMARKET_CATEGORY_TAGS):
                    continue

            # Topic keyword filter (user-supplied)
            if topic and topic not in title_lower and topic not in str(tags_labels):
                continue

            slug = event.get('slug', '')
            link = f'https://polymarket.com/event/{slug}' if slug else ''

            probs: list[str] = []
            for m in event.get('markets', []):
                prices_raw = m.get('outcomePrices', [])
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                for out, price in zip(m.get('outcomes', []), prices):
                    try:
                        probs.append(f"{out}: {float(price) * 100:.1f}%")
                    except (ValueError, TypeError):
                        pass

            parsed.append({
                'title': event.get('title', 'Unknown'),
                'slug': slug,
                'link': link,
                'volume': float(event.get('volume', 0)),
                'probability_context': ' | '.join(probs) if probs else 'N/A',
            })

        # Return sorted by volume desc
        return sorted(parsed, key=lambda e: e.get('volume', 0), reverse=True)

    @staticmethod
    def _web_search(query: str) -> str:
        lines: list[str] = []
        try:
            with DDGS() as ddgs:
                for r in ddgs.news(query, max_results=3):
                    lines.append(f"- {r.get('title')}: {r.get('body')}")
        except Exception:
            pass
        return "\n".join(lines) if lines else "No recent news found."