# utils/prompt_cache.py
"""Zero-dependency prompt cache and macro dirty-flag broker.
No project imports — safe to import from any module without circular dependency risk.
"""
from __future__ import annotations

_cached_prompt: str | None = None
_macros_dirty: bool = True   # True on cold start forces one initial build


def get_cached_prompt() -> str | None:
    return _cached_prompt


def set_cached_prompt(s: str) -> None:
    global _cached_prompt
    _cached_prompt = s


def is_macros_dirty() -> bool:
    return _macros_dirty


def mark_macros_dirty() -> None:
    global _macros_dirty
    _macros_dirty = True


def clear_macros_dirty() -> None:
    global _macros_dirty
    _macros_dirty = False
