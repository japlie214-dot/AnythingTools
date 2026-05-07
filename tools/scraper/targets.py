# tools/scraper/targets.py
"""Site registry and article-body selector constants for the Scraper tool."""

TARGETS = [
    {
        "name": "FT",
        "url": "https://www.ft.com",
        "selectors": [
            # Primary: data attribute + URL pattern (most stable)
            'a[data-trackable="heading-link"][href^="/content/"]',
            # Fallback 1: UUID pattern (FT article IDs are hyphenated UUIDs)
            'a[href^="/content/"][href*="-"]',
            # Fallback 2: data attribute alone
            'a[data-trackable="heading-link"]',
            # Fallback 3: class + URL (keep for backward compat)
            'a.link[href^="/content/"]',
            # Legacy fallbacks (deprecated but harmless)
            "a.js-teaser-heading-link",
            "a.o-teaser__heading-link",
            "div.o-teaser__heading > a",
        ],
        "filter": "/content/",  # Keep existing filter
    },
    {
        "name": "Bloomberg",
        "url": "https://www.bloomberg.com",
        "selectors": ['a[href^="/news/articles/"]'],
        "filter": "",
    },
    {
        "name": "Technoz",
        "url": "https://www.bloombergtechnoz.com",
        "selectors": ['a[href*="detail-"]'],
        "filter": "detail-news",
    },
]

ARTICLE_BODY_SELECTORS = [
    "article",
    "main",
    '[role="main"]',
    ".article-body",
    "#content",
    ".story-body",
]

VALID_TARGET_NAMES = {t["name"] for t in TARGETS}
TARGET_SITE_MAP = {t["name"]: t for t in TARGETS}
