"""bot/orchestrator_core/eviction.py
Budget enforcement for orchestrator context via FIFO eviction."""
from __future__ import annotations
from typing import Any
import config

class BudgetEnforcer:
    def __init__(self, budget: int | None = None):
        self._budget = budget or int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 800000) * 0.8)

    def calculate_total_size(self, context_items: list[dict]) -> int:
        total = 0
        for item in context_items:
            total += item.get("char_count", 0)
            total += item.get("attachment_char_count", 0)
        return total

    def enforce(self, context_items: list[dict]) -> list[dict]:
        if not context_items:
            return []
        items = list(context_items)
        while True:
            total_size = self.calculate_total_size(items)
            if total_size <= self._budget or not items:
                break
            items.pop(0)

        if len(items) == 1:
            item = items[0]
            item_size = item.get("char_count", 0) + item.get("attachment_char_count", 0)
            if item_size > self._budget:
                available = max(self._budget - 1000, 1000)
                item["content"] = item["content"][:available]
                item["char_count"] = len(item["content"])
                item["attachment_char_count"] = 0
        return items
