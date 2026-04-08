# tools/scraper/targets.py
"""Site registry and article-body selector constants for the Scraper tool."""

TARGETS = [
    {
        "name": "FT",
        "url": "https://www.ft.com",
        "selectors": [
            "a.js-teaser-heading-link",
            "a.o-teaser__heading-link",
            "div.o-teaser__heading > a",
            "div.js-teaser-headline > a",
            'a[data-trackable="heading-link"]',
        ],
        "filter": "/content/",
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
