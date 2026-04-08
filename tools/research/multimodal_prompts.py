# tools/research/multimodal_prompts.py

MULTIMODAL_VISION_SYSTEM_PROMPT = "You are a document analysis expert. Extract all text content, reconstruct tables accurately, and describe charts/diagrams using Markdown formatting. Preserve the original layout and structure."

MULTIMODAL_VISION_USER_PROMPT = """Extract all text, tables, and structured content from this document.
                    
INSTRUCTIONS:
1. Preserve original formatting and layout.
2. Reconstruct tables using Markdown table syntax.
3. Use appropriate headings for sections.
4. Describe key visual elements (charts, graphs, diagrams).
5. Maintain reading order and document structure.
6. Include all numerical data and labels.
7. Use bullet points for lists.
8. Preserve financial data precision.

### DOCUMENT
"""

