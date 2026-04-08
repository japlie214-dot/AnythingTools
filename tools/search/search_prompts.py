# tools/search/search_prompts.py

SEARCH_INITIAL_PROMPT = """Provide a factual, evidence-based answer citing the URLs.

### QUERY
{current_query}

### CONTEXT
{context}
###"""

SEARCH_EVALUATION_VERBOSE_PROMPT = """Evaluate if the collected evidence is sufficient and balanced for the goal.

INSTRUCTIONS:
1. If the evidence is sufficient, output exactly: <satisfied>Yes</satisfied>
2. If the evidence is insufficient, output a concise refinement query inside <refinement> tags.
3. Ensure the refinement query uses exclusively 3 to 8 keywords.
4. Frame the refinement as a search engine query (e.g., use keywords instead of conversational phrasing or questions).

### INITIAL GOAL
{initial_query}

### EVIDENCE SO FAR
{search_history}
###"""

SEARCH_FINAL_SYNTHESIS_PROMPT = """Synthesise a comprehensive, well-cited final answer in Markdown based on the research history.

### GOAL
{initial_query}

### RESEARCH HISTORY
{search_history}
###"""

