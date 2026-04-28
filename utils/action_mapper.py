# utils/action_mapper.py
"""
Bidirectional resolution between bid strings and Botasaurus CSS selector actions.

This module provides a clean interface for mapping between the flat bid namespace
(data-ai-id="bid_N") and actionable CSS selectors for Botasaurus operations.
"""

from botasaurus.browser import Driver

BID_ATTR = "data-ai-id"

def _resolve(bid: str | int) -> str:
    """Convert a bid string or numeric ID to a CSS selector."""
    bid_str = str(bid)
    if bid_str.isdigit():
        bid_str = f"bid_{bid_str}"
    
    safe = bid_str.replace('"', '\\"').replace("'", "\\'")
    return f'[{BID_ATTR}="{safe}"]'

def _elem_exists(driver: Driver, selector: str) -> bool:
    """
    Check if element exists with the given selector.
    
    Args:
        driver: Botasaurus Driver instance
        selector: CSS selector
        
    Returns:
        True if element exists, False otherwise
    """
    try:
        return driver.run_js(f"return !!document.querySelector('{selector}');")
    except Exception:
        return False

def click(driver: Driver, bid: str | int) -> None:
    """
    Click an element by bid.
    
    Args:
        driver: Botasaurus Driver instance
        bid: The bid string (e.g., "bid_0") or numeric ID (e.g., 0)
        
    Raises:
        ValueError: If element with bid doesn't exist
    """
    sel = _resolve(bid)
    if not _elem_exists(driver, sel):
        raise ValueError(f"click: no element with bid {bid}")
    driver.click(sel)

def fill(driver: Driver, bid: str | int, value: str) -> None:
    """
    Fill an input element by bid.
    
    Args:
        driver: Botasaurus Driver instance
        bid: The bid string (e.g., "bid_0") or numeric ID (e.g., 0)
        value: Text to fill
        
    Raises:
        ValueError: If element with bid doesn't exist
    """
    sel = _resolve(bid)
    if not _elem_exists(driver, sel):
        raise ValueError(f"fill: no element with bid {bid}")
    driver.type(sel, value)

def hover(driver: Driver, bid: str | int) -> None:
    """
    Hover over an element by bid.
    
    Args:
        driver: Botasaurus Driver instance
        bid: The bid string (e.g., "bid_0") or numeric ID (e.g., 0)
    """
    sel = _resolve(bid)
    bid_str = str(bid)
    if bid_str.isdigit():
        bid_str = f"bid_{bid_str}"
    driver.run_js(
        f"""
        (function() {{
            var el = document.querySelector('{sel}');
            if (!el) throw new Error('hover: no element with bid {{}}'.format('{bid_str}'));
            var ev = new MouseEvent('mouseover', {{ bubbles: true }});
            el.dispatchEvent(ev);
        }})();
        """
    )