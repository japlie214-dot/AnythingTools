# utils/observation_adapter.py
"""
Botasaurus Observation Adapter for robust SoM extraction.

This module provides a synchronous interface compatible with Botasaurus's run_js method,
with simplified error handling and no watchdog (as run_js is blocking).
"""

import json
import pkgutil
from typing import Any, Dict

from botasaurus.browser import Driver
from utils.logger import get_dual_logger
import config

log = get_dual_logger(__name__)


class MarkingError(Exception):
    """Raised when SoM marking fails."""
    pass


class BotasaurusObservationAdapter:
    """
    Synchronous observation adapter for Botasaurus browser automation.
    Provides pre-extract marking and post-extract cleanup.
    """
    
    def __init__(self, driver: Driver):
        self.driver = driver
        # Load JavaScript assets
        try:
            self._mark_js = pkgutil.get_data("utils", "javascript/frame_mark_elements.js").decode("utf-8")
            self._unmark_js = pkgutil.get_data("utils", "javascript/frame_unmark_elements.js").decode("utf-8")
        except (AttributeError, FileNotFoundError):
            # Fallback for when pkgutil.get_data doesn't work
            import os
            js_dir = os.path.join(os.path.dirname(__file__), "javascript")
            with open(os.path.join(js_dir, "frame_mark_elements.js"), "r") as f:
                self._mark_js = f.read()
            with open(os.path.join(js_dir, "frame_unmark_elements.js"), "r") as f:
                self._unmark_js = f.read()

    def pre_extract(self, lenient: bool = False) -> Dict[str, Any]:
        """
        Inject SoM markers and collect element metadata.
        
        Args:
            lenient: If True, return empty results on failure instead of raising
             
        Returns:
            Dictionary with marked_count, som_count, and last_bid
             
        Raises:
            MarkingError: If marking fails and lenient=False
        """
        try:
            result = self.driver.run_js(self._mark_js, {"bid_attr": "data-ai-id"})
            if not isinstance(result, dict):
                result = json.loads(result) if isinstance(result, str) else {}
            return result
        except Exception as e:
            if lenient:
                return {"marked_count": 0}
            raise MarkingError(str(e))

    def post_extract(self) -> None:
        """
        Clean up temporary DOM attributes.
        Logs warnings on failure but never raises.
        """
        try:
            self.driver.run_js(self._unmark_js, {"bid_attr": "data-ai-id"})
        except Exception:
            pass
