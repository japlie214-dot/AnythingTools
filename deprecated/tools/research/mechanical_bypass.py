# deprecated/tools/research/mechanical_bypass.py
from botasaurus.browser import Driver
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# BOTASAURUS INTERACTION POLICY — READ BEFORE ADDING ANY BROWSER ACTION
# ═══════════════════════════════════════════════════════════════════════════
# ALL click / scroll / type / navigate actions MUST use Botasaurus native API.
# run_js() is ONLY permitted for DOM mutations that have no native equivalent:
#   data-ai-id injection, badge overlay management, MutationObserver,
#   and getBoundingClientRect/elementFromPoint occlusion tests.
# If you reach for run_js() for a click, scroll, or type — STOP.
# ═══════════════════════════════════════════════════════════════════════════


def attempt_mechanical_bypass(driver: Driver, headless: bool) -> bool:  # noqa: ARG001
    # `headless` is accepted for API consistency with the other extracted
    # module functions but is not used by this function body. It is available
    # for future differentiation (e.g., skipping body-click fallback in
    # headless mode) without a signature change.
    attempts_made = False

    if hasattr(driver, "enable_human_mode"):
        driver.enable_human_mode()
    driver.short_random_sleep()

    selectors_to_try = [
        # Cloudflare Turnstile-specific selectors — highest priority.
        # Restricted to known-interactive element types to prevent a false-positive
        # attempts_made=True from clicking a non-actionable wrapper div.
        "button.cf-turnstile",
        "input.cf-turnstile",
        'iframe[src*="turnstile"]',
        "#turnstile-wrapper",
        # Cookie consent and generic accept selectors
        'button[aria-label*="Accept"]',
        "button#accept-cookies",
        "button#cookie-accept",
        "button#agree",
        "button#consent",
        ".cookie-accept",
        ".accept-cookies",
        'input[type="checkbox"]',
        "div.checkbox",
        'button:contains("Accept")',
        'button:contains("Agree")',
        'button:contains("Continue")',
        'button:contains("I Agree")',
    ]
    for selector in selectors_to_try:
        try:
            if driver.is_element_present(selector):
                driver.click(selector)
                driver.short_random_sleep()
                attempts_made = True
                log.dual_log(
                    tag="Research:Scraper",
                    message=f"Mechanical bypass: clicked {selector}",
                    level="INFO",
                    payload={"selector": selector},
                )
                break
        except Exception as e:
            log.dual_log(
                tag="Research:Scraper",
                message=f"Selector {selector} failed: {e}",
                level="DEBUG",
                payload={"selector": selector, "error": repr(e)},
            )
            continue

    if not attempts_made:
        try:
            # Botasaurus native click — never use run_js to invoke .click() on DOM elements.
            # driver.click() already waits for the element to be clickable and
            # mimics a real mouse event, making JS-based clicks both redundant and weaker.
            if driver.is_element_present("button"):
                driver.click("button")
                attempts_made = True
                log.dual_log(
                    tag="Research:Scraper",
                    message="Mechanical bypass: clicked first visible button",
                    level="INFO",
                )
        except Exception:
            pass

    if not attempts_made:
        try:
            # Botasaurus native click — never use run_js("document.body.click()").
            driver.click("body")
            driver.short_random_sleep()
            attempts_made = True
            log.dual_log(
                tag="Research:Scraper",
                message="Mechanical bypass: clicked body to dismiss overlays",
                level="INFO",
            )
        except Exception:
            pass

    if not attempts_made:
        try:
            # Botasaurus native coordinate resolution for invisible Turnstile iframes.
            # To click a specific coordinate point without using JS, we resolve the element
            # at that point using Botasaurus native API and dispatch a native click.
            # MAINTENANCE NOTE: re-validate coordinates if window_size or Turnstile CSS changes.
            target = driver.get_element_at_point(160, 290)
            if target:
                target.click()
                driver.short_random_sleep()
                attempts_made = True
                log.dual_log(
                    tag="Research:Scraper",
                    message="Mechanical bypass: clicked Turnstile area via coordinate fallback",
                    level="INFO",
                    payload={"x": 160, "y": 290},
                )
        except Exception:
            pass

    # Unconditional human-mode release — executes regardless of attempts_made value.
    if hasattr(driver, "disable_human_mode"):
        driver.disable_human_mode()

    return attempts_made
