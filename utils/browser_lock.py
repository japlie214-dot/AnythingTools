# utils/browser_lock.py
"""
Singleton asyncio Lock for browser-capable tools.

Purpose:
- Ensures mutual exclusion across ResearchTool, ScraperTool, and IBKRTool
- Prevents concurrent browser sessions that would violate the singleton model
- Returns immediate "busy" responses instead of blocking on lock acquisition

Usage:
    from utils.browser_lock import browser_lock
    
    if browser_lock.locked():
        return "⚠️ System is currently busy running another browser task. Please try again later."
    
    await browser_lock.acquire()
    try:
        # ... browser work here ...
    finally:
        browser_lock.release()
"""

import asyncio

class BrowserLockProxy:
    """Lazy proxy for asyncio.Lock to avoid RuntimeError on import outside an event loop."""
    def __init__(self):
        self._lock = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def locked(self) -> bool:
        return self.lock.locked()

    async def acquire(self):
        return await self.lock.acquire()

    def release(self):
        self.lock.release()

# One lock governs all browser-capable tools.
browser_lock = BrowserLockProxy()
