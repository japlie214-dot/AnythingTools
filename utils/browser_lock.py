# utils/browser_lock.py
"""
Singleton threading.Lock for browser-capable tools.

Purpose:
- Ensures mutual exclusion across ResearchTool, ScraperTool, and IBKRTool
- Prevents concurrent browser sessions that would violate the singleton model
- Returns immediate "busy" responses instead of blocking on lock acquisition

Usage:
    from utils.browser_lock import browser_lock
    
    if browser_lock.locked():
        return "⚠️ System is currently busy running another browser task. Please try again later."
    
    browser_lock.acquire()
    try:
        # ... browser work here ...
    finally:
        browser_lock.release()
"""

import threading

class BrowserLockProxy:
    """Proxy for threading.Lock for safe cross-thread browser synchronization."""
    def __init__(self):
        self._lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def locked(self) -> bool:
        return self.lock.locked()

    def acquire(self):
        return self.lock.acquire()

    def release(self):
        self.lock.release()

    def safe_release(self):
        if self.locked():
            try:
                self.lock.release()
            except RuntimeError:
                pass

# One lock governs all browser-capable tools.
browser_lock = BrowserLockProxy()
