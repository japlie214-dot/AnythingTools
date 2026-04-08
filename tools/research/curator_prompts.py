# tools/research/curator_prompts.py

CURATOR_EXTRACTION_PROMPT = """You are a Master Knowledge Curator for institutional analysis.
Review the completed research report and extract timeless, durable knowledge that should be preserved in long-term memory.

INSTRUCTIONS:
1. Identify knowledge facts that are valuable beyond this specific research session.
2. Distinguish between:
   - "Knowledge": Factual information about topics, strategies, market conditions
   - "Values": Strategic principles, assessment criteria, analytical frameworks
3. For each memory, provide a concise topic and clear, standalone memory text.
4. Focus exclusively on permanent facts and principles; omit any temporary information like specific dates, prices, or transient data.

OUTPUT FORMAT (inline JSON):
Provide the output in valid JSON matching the structure below.
{{
  "memories": [
    {{
      "decision": "New" | "Update",
      "type": "Knowledge" | "Values",
      "topic": "brief topic identifier",
      "final_memory": "concise, durable insight or fact"
    }}
  ]
}}

EXAMPLES:
- GOOD: "Factual Baseline extraction is the critical first step in institutional analysis"
- GOOD: "When financial ratios deviate from industry averages by >20%, investigate underlying causes"
- BAD: "Company X reported $1.2B revenue in Q3 2024" (too time-specific)
- BAD: "Stock price was $156.43 on Oct 15" (transient data)

### REPORT TO REVIEW
{report}
###
"""
