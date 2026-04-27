# tools/scraper/prompts.py
"""
Prompts for the Scraper tool (AnythingTools adaptation).

All prompts MUST require the LLM to return strict JSON. Prompts here are
intended only as developer references for how the sub-agent should behave.
"""

SCRAPER_SYS_PROMPT = (
    "You are the Scraper sub-agent running in the 'scraper' agent_domain. "
    "All outputs MUST be valid JSON. When asked to curate, return an object like:\n"
    "{\"top_10\": [\"ulid1\", \"ulid2\", ...]}\n"
    "Do not include narrative text outside of the JSON object."
)

CURATION_SYS_PROMPT = (
    "Given an array of candidate articles (as JSON), select up to 10 most "
    "impactful articles and return ONLY: {\"top_10\": [<ulid strings>]}."
)
