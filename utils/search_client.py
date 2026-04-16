# utils/search_client.py
import time
from typing import List
from ddgs import DDGS
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class SearchClient:
    _DDGS_MAX_RETRIES = 2
    _DDGS_RETRY_DELAY_S = 3

    @staticmethod
    def fetch_text_and_news(query: str, max_text: int = 5, max_news: int = 3) -> List[str]:
        text_res: List[str] = []
        news_res: List[str] = []
        seen: set[str] = set()

        def normalize(u: str) -> str:
            return u.split("?")[0].strip("/")

        for attempt in range(SearchClient._DDGS_MAX_RETRIES + 1):
            try:
                with DDGS() as ddgs:
                    if max_text > 0:
                        for r in ddgs.text(query, region="wt-wt", safesearch="moderate", max_results=max_text):
                            url = r.get("href")
                            if url and normalize(url) not in seen:
                                seen.add(normalize(url))
                                text_res.append(f"Title: {r.get('title')}\nURL: {url}\nSnippet: {r.get('body')}")
                    if max_news > 0:
                        for r in ddgs.news(query, region="wt-wt", safesearch="moderate", max_results=max_news):
                            url = r.get("url") or r.get("href")
                            if url and normalize(url) not in seen:
                                seen.add(normalize(url))
                                news_res.append(f"Title: {r.get('title')}\nURL: {url}\nSnippet: {r.get('body')}")
                return text_res + news_res
            except Exception as exc:
                if attempt == SearchClient._DDGS_MAX_RETRIES:
                    log.dual_log(tag="SearchClient", message=f"DDGS exhausted retries for '{query}'", level="ERROR", exc_info=True)
                    break
                time.sleep(SearchClient._DDGS_RETRY_DELAY_S)
        return []
