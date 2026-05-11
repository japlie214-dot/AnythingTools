# tools/scraper/paywall.py
"""
Paywall detection module using fragment matching to detect paywalls
even when HTML structure is fragmented across multiple elements.
"""

import re
from dataclasses import dataclass
from bs4 import BeautifulSoup
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


@dataclass
class PaywallResult:
    is_paywalled: bool
    detected_indicators: list[str]
    blocker_type: str = "paywall"


class PaywallDetector:
    """
    Detects paywalls by scanning for small text fragments that indicate
    subscription requirements. Uses multiple independent fragments to
    resist HTML element splits and structural changes.
    """
    
    # Financial Times fragments - small, independent pieces
    FT_FRAGMENTS = [
        "per month",
        "complete digital access",
        "cancel anytime",
        "unlimited access",
        "subscribe to",
    ]
    
    # Bloomberg fragments
    BLOOMBERG_FRAGMENTS = [
        "explore our full range",
        "keep reading for",
        "premium access",
        "subscription required",
    ]
    
    # Generic paywall indicators
    GENERIC_FRAGMENTS = [
        "paywall",
        "premium content",
        "subscription required",
        "log in to read",
        "sign up to continue",
        "membership required",
    ]

    def detect(self, html_content: str) -> PaywallResult:
        """
        Scan HTML content for paywall indicators.
        
        Args:
            html_content: Raw HTML string to analyze
            
        Returns:
            PaywallResult indicating if paywall detected and which fragments matched
        """
        if not html_content:
            return PaywallResult(is_paywalled=False, detected_indicators=[])
            
        # Parse HTML and remove non-visible elements
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]):
            tag.decompose()
            
        # Extract all visible text
        text = soup.get_text(separator=" ", strip=True).lower()
        
        if not text:
            return PaywallResult(is_paywalled=False, detected_indicators=[])
        
        # Check Financial Times (requires 2+ matches for confidence)
        ft_matches = [f for f in self.FT_FRAGMENTS if f in text]
        if len(ft_matches) >= 2:
            log.dual_log(
                tag="Scraper:Paywall:Detected",
                message="Financial Times paywall detected",
                level="INFO",
                payload={"indicators": ft_matches}
            )
            return PaywallResult(is_paywalled=True, detected_indicators=ft_matches, blocker_type="paywall")
            
        # Check Bloomberg (requires 1+ matches)
        bb_matches = [f for f in self.BLOOMBERG_FRAGMENTS if f in text]
        if bb_matches:
            log.dual_log(
                tag="Scraper:Paywall:Detected",
                message="Bloomberg paywall detected",
                level="INFO",
                payload={"indicators": bb_matches}
            )
            return PaywallResult(is_paywalled=True, detected_indicators=bb_matches, blocker_type="paywall")
            
        # Check generic fragments (requires 2+ matches to avoid false positives)
        generic_matches = [f for f in self.GENERIC_FRAGMENTS if f in text]
        if len(generic_matches) >= 2:
            b_type = "gatekeeper" if any(kw in text for kw in ["log in", "sign up", "continue"]) else "paywall"
            log.dual_log(
                tag="Scraper:Paywall:Detected",
                message=f"Generic paywall/gatekeeper detected ({b_type})",
                level="INFO",
                payload={"indicators": generic_matches, "blocker_type": b_type}
            )
            return PaywallResult(is_paywalled=True, detected_indicators=generic_matches, blocker_type=b_type)
            
        return PaywallResult(is_paywalled=False, detected_indicators=[])
