# tools/scraper/summary_prompts.py

MAP_REDUCE_SUMMARIZE_CHUNK_PROMPT = """Extract the key institutional knowledge from this text segment.

INSTRUCTIONS:
1. Identify and extract discrete facts, data points, causal claims, and named entities.
2. Each extracted item must be a standalone factual statement — no attribution phrases.
3. Preserve exact numbers, names, dates, and units.

### TEXT SEGMENT (PART {index})
{chunk_text}
###"""

MAP_REDUCE_SYNTHESIZE_PROMPT = """Synthesize the following extracted knowledge segments into one cohesive intelligence briefing.

INSTRUCTIONS:
1. Merge duplicate facts, resolve contradictions using later-segment data as authoritative.
2. Organize knowledge bullets by thematic coherence and chronological order.
3. Each bullet must remain a standalone fact — no "the article says" or narrative connectors.

### EXTRACTED SEGMENTS
{combined_summaries}
###"""

