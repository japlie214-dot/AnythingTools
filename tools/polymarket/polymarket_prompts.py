# tools/polymarket/polymarket_prompts.py

POLYMARKET_EVALUATION_PROMPT = """Evaluate if the search history provides balanced evidence for the market outcomes.

INSTRUCTIONS:
1. If the evidence is sufficient, reply exactly with: <satisfied>Yes</satisfied>
2. If the evidence is insufficient, reply with a specific search query inside <refinement> tags.

### GOAL
{title}

### CONTEXT
{context}

### SEARCH HISTORY
{search_history}
###"""

POLYMARKET_SYNTHESIS_PROMPT = """You are a Market Analyst.
Write a structured research report based on the provided event, probabilities, and evidence.

INSTRUCTIONS:
1. Output strictly in Markdown format.
2. Follow the EXACT structure shown in the EXPECTED FORMAT below.
3. Synthesize the evidence into cohesive bullet points.

EXPECTED FORMAT:
*### {title}*
**Market At-a-Glance:** {context}
# Key Arguments
- [Synthesized Bullet points]
# Influencing Factors
- [Synthesized Bullet points]

Conclusion: [Final Synthesis]

Sources: [List unique sources here]

### EVIDENCE
{evidence}
###"""

