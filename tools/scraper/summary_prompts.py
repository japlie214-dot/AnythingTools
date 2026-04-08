# tools/scraper/summary_prompts.py

MAP_REDUCE_SUMMARIZE_CHUNK_PROMPT = """Summarize the key institutional intelligence in this text segment.

INSTRUCTIONS:
1. Extract and summarize the most important facts and insights.
2. Ensure the summary is concise and objective.

### TEXT SEGMENT (PART {index})
{chunk_text}
###"""

MAP_REDUCE_SYNTHESIZE_PROMPT = """Synthesize the following chronologically summarized segments into one cohesive master briefing.

INSTRUCTIONS:
1. Combine the insights into a flowing, logical briefing.
2. Maintain chronological and thematic coherence.

### SUMMARIZED SEGMENTS
{combined_summaries}
###"""

