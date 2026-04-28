# utils/som_utils.py
"""Set-of-Marks (SoM) utilities for browser automation.

This module provides functions to inject data-ai-id attributes, remove unwanted overlays,
and run DOM mutations that do not have native Botasaurus equivalents.

PLANNED EXPANSION: This is where single-tab policy enforcement is centralized to ensure
a consistent single-window driver experience across tools. See `enforce_single_tab()`.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from botasaurus.browser import Driver
from utils.logger import get_dual_logger
from utils.text_processing import clean_html_for_agent

log = get_dual_logger(__name__)


# ── Marker injection & cleanup (allowed run_js use-cases) ─────────────────────


def inject_som(driver: Driver, start_id: int = 1) -> int:
    """Inject data-ai-id attributes with visual badges and return the last ID used."""
    try:
        from utils.som_injector import SoMInjector, InjectionMode, SoMCriticalTimeoutError
        injector = SoMInjector(driver, timeout=60.0)
        try:
            return injector.inject(start_id=start_id, mode=InjectionMode.FULL)
        except SoMCriticalTimeoutError:
            raise  # Let the orchestrator or daemon catch this to perform surgical kill
        except Exception as e:
            log.dual_log(tag="SoM:Inject", message=f"Full injection failed: {e}. Retrying in marker-only mode.", level="WARNING")
            return injector.inject(start_id=start_id, mode=InjectionMode.MARKER_ONLY)
    except Exception as e:
        log.dual_log(tag="SoM:Inject", message=f"SoM injection failed entirely: {e}", level="ERROR")
        return start_id


def wait_for_dom_stability(driver: Driver, timeout: int = 10):
    """Wait for the DOM to stabilize using adaptive CDP checks."""
    import time
    try:
        if driver.run_js("return document.readyState") == "complete":
            time.sleep(0.3)
    except Exception:
        pass
        
    last_count = 0
    start_time = time.time()
    stable_count = 0
    
    while time.time() - start_time < timeout:
        try:
            current_count = driver.run_js("return document.querySelectorAll('*').length")
        except Exception:
            time.sleep(0.5)
            continue
            
        if current_count == last_count and current_count > 0:
            stable_count += 1
            if stable_count >= 2:
                break
        else:
            stable_count = 0
            last_count = current_count
        time.sleep(0.5)


def extract_surgical_html(driver: Driver) -> str:
    """Returns readable HTML while preserving data-ai-id attributes, applying configured character budget."""
    from utils.text_processing import clean_html_for_agent
    import config
    budget = getattr(config, "BROWSER_SOM_HTML_CHAR_BUDGET", 20000)
    raw_html = driver.page_html or ""
    return clean_html_for_agent(raw_html, max_chars=budget, extra_attrs={"data-ai-id"})


def reinject_all(driver: Driver, id_tracking: dict) -> None:
    """Full rebuild: wait for DOM stability, inject main document, and track ID ranges."""
    wait_for_dom_stability(driver)
    remove_overlays(driver)
    
    start_id = 1
    last_id = inject_som(driver, start_id)
    id_tracking['main'] = (start_id, last_id - 1)


def inject_ai_ids(driver: Driver) -> None:
    """Legacy wrapper—kept as alias for backward compatibility."""
    inject_som(driver)


def remove_overlays(driver: Driver) -> None:
    """Remove common overlay and badge elements via allowed run_js."""
    try:
        driver.run_js(
            """(function(){
                var selectors = [
                    '[data-ai-badge]',
                    '.cookie-banner',
                    '.overlay',
                    '.modal-backdrop',
                    '[aria-hidden="true"]'
                ];
                selectors.forEach(function(sel) {
                    try {
                        document.querySelectorAll(sel).forEach(function(el) { el.remove(); });
                    } catch (e) {}
                });
            })();"""
        )
    except Exception as e:
        log.dual_log(tag="SoM:Overlays", message=f"Failed to remove overlays: {e}", level="WARNING", exc_info=e)


def read_visible_html(driver: Driver) -> str:
    """Return cleaned visible HTML for LLM context width."""
    try:
        raw = driver.page_html or ""
        return clean_html_for_agent(raw)
    except Exception as e:
        log.dual_log(tag="SoM:ReadHTML", message=f"Failed to read HTML: {e}", level="WARNING", exc_info=e)
        return ""


def get_element_by_hint(driver: Driver, hint: str) -> Tuple[str | None, str | None]:
    """Best-effort CSS locator for a human hint (e.g., 'Accept', 'Login'). Lower-case matching."""
    try:
        candidates = [
            f"button:contains('{hint}')",
            f"a:contains('{hint}')",
            f"[aria-label*='{hint}']",
            f"[title*='{hint}']",
            f"[data-ai-text*='{hint}']",
        ]
        for css in candidates:
            try:
                if driver.run_js(f"return document.querySelector('{css}')"):
                    return css, "css"
            except Exception:
                continue
        # Fallback scan for data-ai-id text inclusion
        js = """(function(){
            var nodes = document.querySelectorAll('[data-ai-id]');
            for (var i=0; i<nodes.length; i++){
                if (nodes[i].innerText && nodes[i].innerText.toLowerCase().includes(%s)){
                    return nodes[i].getAttribute('data-ai-id');
                }
            }
            return null;
        })();""" % (repr(hint.lower()))
        ai_id = driver.run_js(js)
        if ai_id:
            return f'[data-ai-id="{ai_id}"]', 'data-ai-id'
    except Exception as e:
        log.dual_log(tag="SoM:Locator", message=f"Locator failed for hint '{hint}': {e}", level="WARNING", exc_info=e)
    return None, None


# ── Single-Tab Policy Helpers (planned canonical enforcement) ─────────────────


def enforce_single_tab(driver: Driver) -> None:
    """Enforce single-tab policy by overriding window.open and stripping target="_blank" from anchors.

    This uses allowed run_js DOM mutations. It should be called immediately after
    a navigation or before user interaction completes to reduce the chance of
    new tabs popping.
    """
    try:
        # Override window.open to route calls to current location
        driver.run_js(
            """(function(){
                if (!window.__singleTabPatched) {
                    window.__singleTabPatched = true;
                    var realOpen = window.open;
                    window.open = function(url, target, features) {
                        if (url) window.location.href = url;
                        return window; // Maintain API return shape
                    };
                }
            })();"""
        )
        # Remove target="_blank" from all anchors in the current document
        driver.run_js(
            """(function(){
                var anchors = document.querySelectorAll('a[target="_blank"]');
                for (var i=0; i<anchors.length; i++) anchors[i].removeAttribute('target');
            })();"""
        )
    except Exception as e:
        # Fail silently with a log; non-fatal to the tool
        log.dual_log(tag="SingleTab", message=f"Failed to enforce single-tab policy: {e}", level="WARNING")


# ── MutationObserver for DOM stability (allowed run_js) ───────────────────────


def setup_dom_stability_observer(driver: Driver) -> None:
    """Run a MutationObserver to monitor DOM changes and reapply overlays removal if needed."""
    try:
        driver.run_js(
            """(function(){
                if (window.__dom_observer) return;
                var target = document.body || document.documentElement;
                window.__dom_observer = new MutationObserver(function(mutations){
                    // Re-run removal of known overlay classes if they appear
                    var overlays = document.querySelectorAll('[data-ai-badge], .overlay, .modal-backdrop');
                    overlays.forEach(function(el){ el.remove(); });
                });
                if (target) window.__dom_observer.observe(target, { childList: true, subtree: true });
            })();"""
        )
    except Exception as e:
        log.dual_log(tag="SoM:Observer", message=f"Failed to set up DOM observer: {e}", level="WARNING", exc_info=e)
