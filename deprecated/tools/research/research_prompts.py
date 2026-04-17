# deprecated/tools/research/research_prompts.py

STEP_PROMPTS = {
    "factual_baseline": "Factual Baseline",
    "strategic_analysis": "Strategic Analysis",
    "growth_areas": "Growth Areas",
    "resilience": "Resilience Analysis",
    "causal_chains": "Causal Chains",
    "scenarios": "Scenario Projections",
    "uncertainties": "Key Uncertainties",
    "executive_summary": "Executive Summary"
}


def get_cot_prompt(step: str, raw_text: str, accumulated_xml: str = "", institutional_context: str = "") -> str:
    base_instructions = (
        "You are an expert institutional research analyst. "
        "Analyze the provided text and generate structured output exclusively in XML format.\n\n"
        "INSTRUCTIONS:\n"
        "1. Use the following tags: <section>, <heading>, <paragraph>, <list>, <li>, <table>, <row>, <cell>.\n"
        "2. Wrap your analysis in <section> tags with a 'type' attribute indicating the analysis phase.\n"
        "3. Ensure all XML is valid and properly nested.\n"
        "4. Be concise but comprehensive."
    )
    
    context_injection = ""
    if institutional_context:
        context_injection = f"\n### INSTITUTIONAL CONTEXT\n{institutional_context}\n"
    
    prompts = {
        "factual_baseline": f"""{base_instructions}\n\nSTEP 1/8: Factual Baseline Extraction\n\nFocus on:\n- Company/organization name and basic details\n- Core business activities and products/services\n- Key financial metrics (if available)\n- Market position and competitive landscape\n- Recent developments or announcements\n\nEXPECTED FORMAT:\n<section type=\"factual_baseline\">\n  <heading type=\"h2\">Factual Baseline</heading>\n  <paragraph>Key factual information...</paragraph>\n  <list>\n    <li>Specific fact 1</li>\n    <li>Specific fact 2</li>\n  </list>\n</section>\n{context_injection}\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "strategic_analysis": f"""{base_instructions}\n\nSTEP 2/8: Strategic Analysis\n\nFocus on:\n- Strategic positioning in the market\n- Key competitive advantages or moats\n- Strategic initiatives or direction\n- Stakeholder relationships and value proposition\n- Strategic risks and opportunities\n\nEXPECTED FORMAT:\n<section type=\"strategic_analysis\">\n  <heading type=\"h2\">Strategic Analysis</heading>\n  <paragraph>Strategic insights...</paragraph>\n  <list>\n    <li>Strategic point 1</li>\n    <li>Strategic point 2</li>\n  </list>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "growth_areas": f"""{base_instructions}\n\nSTEP 3/8: Growth Areas Identification\n\nFocus on:\n- Emerging market opportunities\n- Product/service expansion possibilities\n- Geographic expansion potential\n- New customer segments\n- Innovation and R&D opportunities\n- Partnership and acquisition possibilities\n\nEXPECTED FORMAT:\n<section type=\"growth_areas\">\n  <heading type=\"h2\">Growth Areas</heading>\n  <paragraph>Growth opportunities analysis...</paragraph>\n  <list>\n    <li>Growth opportunity 1 with rationale</li>\n    <li>Growth opportunity 2 with rationale</li>\n  </list>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "resilience": f"""{base_instructions}\n\nSTEP 4/8: Resilience Analysis\n\nFocus on:\n- Financial resilience and liquidity position\n- Operational robustness and redundancy\n- Market volatility tolerance\n- Supply chain dependencies and risks\n- Regulatory compliance and governance\n- Crisis management capabilities\n\nEXPECTED FORMAT:\n<section type=\"resilience\">\n  <heading type=\"h2\">Resilience Analysis</heading>\n  <paragraph>Resilience assessment...</paragraph>\n  <list>\n    <li>Resilience strength 1</li>\n    <li>Resilience weakness 1</li>\n  </list>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "causal_chains": f"""{base_instructions}\n\nSTEP 5/8: Causal Chain Analysis\n\nFocus on:\n- How strategic decisions create operational outcomes\n- How market position affects financial performance\n- How competitive dynamics drive strategic choices\n- How internal capabilities enable or constrain growth\n- How external factors influence performance\n\nEXPECTED FORMAT:\n<section type=\"causal_chains\">\n  <heading type=\"h2\">Causal Chains</heading>\n  <paragraph>Causal relationship analysis...</paragraph>\n  <table>\n    <row>\n      <cell>Cause</cell>\n      <cell>Effect</cell>\n      <cell>Impact Level</cell>\n    </row>\n    <row>\n      <cell>Specific cause</cell>\n      <cell>Specific effect</cell>\n      <cell>High/Med/Low</cell>\n    </row>\n  </table>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "scenarios": f"""{base_instructions}\n\nSTEP 6/8: Scenario Projections\n\nFocus on:\n- Base case scenario (likely continuation)\n- Bull case scenario (optimistic outcomes)\n- Bear case scenario (pessimistic outcomes)\n- Key drivers that would lead to each scenario\n- Probability assessment and implications\n\nEXPECTED FORMAT:\n<section type=\"scenarios\">\n  <heading type=\"h2\">Scenario Projections</heading>\n  <paragraph>Scenario analysis summary...</paragraph>\n  <list>\n    <li>Base case: [description]</li>\n    <li>Bull case: [description]</li>\n    <li>Bear case: [description]</li>\n  </list>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "uncertainties": f"""{base_instructions}\n\nSTEP 7/8: Key Uncertainties\n\nFocus on:\n- Market and competitive uncertainties\n- Regulatory and policy uncertainties\n- Technology and innovation uncertainties\n- Internal operational uncertainties\n- Macroeconomic uncertainties\n- Mitigation strategies for key uncertainties\n\nEXPECTED FORMAT:\n<section type=\"uncertainties\">\n  <heading type=\"h2\">Key Uncertainties</heading>\n  <paragraph>Uncertainty analysis...</paragraph>\n  <list>\n    <li>Uncertainty 1 - Impact: [High/Med/Low] - Likelihood: [High/Med/Low]</li>\n    <li>Uncertainty 2 - Impact: [High/Med/Low] - Likelihood: [High/Med/Low]</li>\n  </list>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section""",
        "executive_summary": f"""{base_instructions}\n\nSTEP 8/8: Executive Summary\n\nFocus on:\n- Key findings and insights\n- Strategic recommendations\n- Critical success factors\n- Key risks and mitigations\n- Action items and next steps\n\nEXPECTED FORMAT:\n<section type=\"executive_summary\">\n  <heading type=\"h1\">Executive Summary</heading>\n  <paragraph>Comprehensive synthesis of all analysis...</paragraph>\n  <list>\n    <li>Key finding 1</li>\n    <li>Key finding 2</li>\n    <li>Recommendation 1</li>\n  </list>\n  <page_break/>\n</section>\n{context_injection}\n### PREVIOUS ANALYSIS\n{accumulated_xml}\n\n### TEXT TO ANALYZE\n{raw_text[:15000]}\n###\n<section"""
    }
    
    return prompts.get(step, f"Unknown step: {step}")


def get_all_steps() -> list:
    return list(STEP_PROMPTS.keys())


SCRAPER_RECOVERY_PROMPT = """You are an AI Navigator agent helping a web scraper bypass popups, cookie consent walls, or CAPTCHAs.
The target content type is: {topic_hint}

Instructions:
"""

