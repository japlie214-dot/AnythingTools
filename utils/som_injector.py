"""utils/som_injector.py
Chunked SoM injection engine with bounded execution and graceful degradation.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple, Dict, Optional
from botasaurus.browser import Driver
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)

class SoMCriticalTimeoutError(Exception):
    """Raised when JS execution completely hangs, requiring browser surgical_kill."""
    def __init__(self, message: str, phase: str = "unknown"):
        super().__init__(message)
        self.phase = phase

class InjectionMode(Enum):
    FULL = "full"               
    MARKER_ONLY = "marker_only" 
    DEGRADED = "degraded"       

class WatchdogTimer:
    """Cooperative timeout mechanism for run_js() calls. Sets a flag if timeout reached."""
    def __init__(self, timeout_seconds: float = 60.0):
        self._timeout = timeout_seconds
        self._timed_out = threading.Event()
        self._timer: Optional[threading.Timer] = None

    def __enter__(self):
        self._timed_out.clear()
        self._timer = threading.Timer(self._timeout, self._timed_out.set)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *args):
        if self._timer:
            self._timer.cancel()

    @property
    def timed_out(self) -> bool:
        return self._timed_out.is_set()

@dataclass
class ElementInfo:
    index: int
    tag: str
    top: float
    left: float
    width: float
    height: float

@dataclass
class ScanResult:
    elements: List[ElementInfo] = field(default_factory=list)
    scan_duration_ms: float = 0.0

class BadgePositionCalculator:
    """Computes badge positions with overlap displacement in Python, avoiding O(n^2) DOM reflows."""
    OVERLAP_THRESHOLD_PX = 15
    DISPLACEMENT_PX = 15

    @staticmethod
    def compute_positions(elements: List[ElementInfo]) -> Dict[int, Tuple[float, float]]:
        placed: List[Tuple[float, float]] = []
        positions: Dict[int, Tuple[float, float]] = {}

        for el in elements:
            top = el.top
            left = el.left
            for p_top, p_left in placed:
                if (abs(p_top - top) < BadgePositionCalculator.OVERLAP_THRESHOLD_PX and
                    abs(p_left - left) < BadgePositionCalculator.OVERLAP_THRESHOLD_PX):
                    top += BadgePositionCalculator.DISPLACEMENT_PX

            positions[el.index] = (top, left)
            placed.append((top, left))
        return positions

class SoMInjector:
    _SCAN_JS = """
    (function(){
        var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, null, false);
        var results = [];
        var idx = 0;
        while (walker.nextNode()) {
            var el = walker.currentNode;
            var rect = el.getBoundingClientRect();
            var style = window.getComputedStyle(el);
            if (el.offsetParent !== null && rect.width > 0 && rect.height > 0 && style.opacity !== '0' && style.visibility !== 'hidden') {
                results.push({
                    index: idx,
                    tag: el.tagName,
                    top: Math.round(rect.top + window.scrollY),
                    left: Math.round(rect.left + window.scrollX),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height)
                });
            }
            idx++;
        }
        return JSON.stringify({elements: results});
    })();
    """

    _MARK_IDS_JS_TEMPLATE = """
    (function(markData){
        for (var i = 0; i < markData.length; i++) {
            var item = markData[i];
            var el = document.querySelector('[data-ai-scan-idx="' + item.scanIdx + '"]');
            if (el) el.setAttribute('data-ai-id', String(item.aiId));
        }
        return markData.length;
    })(%s);
    """

    _MARK_BADGES_JS_TEMPLATE = """
    (function(badgeData){
        var frag = document.createDocumentFragment();
        for (var i = 0; i < badgeData.length; i++) {
            var item = badgeData[i];
            var badge = document.createElement('div');
            badge.setAttribute('data-ai-badge', 'true');
            badge.textContent = String(item.aiId);
            badge.style.position = 'absolute';
            badge.style.backgroundColor = 'red';
            badge.style.color = 'white';
            badge.style.fontSize = '12px';
            badge.style.padding = '2px 4px';
            badge.style.zIndex = '2147483647';
            badge.style.pointerEvents = 'none';
            badge.style.top = item.top + 'px';
            badge.style.left = item.left + 'px';
            frag.appendChild(badge);
        }
        document.body.appendChild(frag);
        return badgeData.length;
    })(%s);
    """

    _TAG_SCAN_INDICES_JS = """
    (function(){
        var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT, null, false);
        var idx = 0;
        while (walker.nextNode()) {
            var el = walker.currentNode;
            var rect = el.getBoundingClientRect();
            var style = window.getComputedStyle(el);
            if (el.offsetParent !== null && rect.width > 0 && rect.height > 0 && style.opacity !== '0' && style.visibility !== 'hidden') {
                el.setAttribute('data-ai-scan-idx', String(idx));
            }
            idx++;
        }
        return idx;
    })();
    """

    def __init__(self, driver: Driver, batch_size: int = 100, timeout: float = 60.0):
        self._driver = driver
        self._batch_size = batch_size
        self._timeout = timeout

    def inject(self, start_id: int = 1, mode: InjectionMode = InjectionMode.FULL) -> int:
        with WatchdogTimer(self._timeout) as wd:
            total_tagged = self._driver.run_js(self._TAG_SCAN_INDICES_JS)
            if wd.timed_out:
                raise SoMCriticalTimeoutError("Scan index tagging timed out", phase="tag_scan_indices")

        if not total_tagged or total_tagged < 1:
            return start_id

        with WatchdogTimer(self._timeout) as wd:
            raw = self._driver.run_js(self._SCAN_JS)
            if wd.timed_out:
                raise SoMCriticalTimeoutError("DOM scan timed out", phase="scan")

        import json
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}

        elements = [ElementInfo(
            index=item["index"], tag=item.get("tag", "unknown"),
            top=item["top"], left=item["left"], width=item["width"], height=item["height"]
        ) for item in data.get("elements", [])]

        if not elements:
            return start_id

        badge_positions = BadgePositionCalculator.compute_positions(elements)

        current_id = start_id
        for batch_start in range(0, len(elements), self._batch_size):
            batch = elements[batch_start:batch_start + self._batch_size]
            mark_data = [{"scanIdx": el.index, "aiId": current_id + i} for i, el in enumerate(batch)]
            
            with WatchdogTimer(self._timeout) as wd:
                self._driver.run_js(self._MARK_IDS_JS_TEMPLATE % json.dumps(mark_data))
                if wd.timed_out:
                    raise SoMCriticalTimeoutError(f"Batch mark ids timed out at idx {batch_start}", phase="mark_ids")
            current_id += len(batch)

        if mode == InjectionMode.FULL:
            current_badge_id = start_id
            for batch_start in range(0, len(elements), self._batch_size):
                batch = elements[batch_start:batch_start + self._batch_size]
                badge_data = []
                for el in batch:
                    pos = badge_positions.get(el.index, (el.top, el.left))
                    badge_data.append({
                        "aiId": current_badge_id,
                        "top": pos[0],
                        "left": pos[1],
                    })
                    current_badge_id += 1
                
                with WatchdogTimer(self._timeout) as wd:
                    self._driver.run_js(self._MARK_BADGES_JS_TEMPLATE % json.dumps(badge_data))
                    if wd.timed_out:
                        raise SoMCriticalTimeoutError(f"Badge injection timed out at idx {batch_start}", phase="mark_badges")

        try:
            self._driver.run_js("""
                var els = document.querySelectorAll('[data-ai-scan-idx]');
                for (var i = 0; i < els.length; i++) els[i].removeAttribute('data-ai-scan-idx');
            """)
        except Exception:
            pass

        return current_id
