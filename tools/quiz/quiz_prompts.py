# tools/quiz/quiz_prompts.py

TRANSLATION_PROMPT = """You are a precise, machine-like translator. 
Translate exclusively the text found inside the `<t>` and `</t>` tags into natural BAHASA INDONESIA.

INSTRUCTIONS:
1. Preserve all structural text and tags exactly as they appear.
2. Use exclusively plain text ASCII characters, avoiding any HTML entities like `
` or `&`.
3. Maintain standard ASCII `"` and `'` instead of typographical quotes.
4. Leave text inside [] or {} exactly as it is without translation.

### TEXT TO TRANSLATE
"""

def prepare_quiz_for_translation(quiz_json: dict) -> str:
    """Wraps values in <t> tags to protect structural keys during LLM translation."""
    tagged_lines = []
    for q in quiz_json.get("questions", []):
        tagged_lines.append(f"Question: <t>{q['q']}</t>")
        for i, opt in enumerate(q['options']):
            tagged_lines.append(f"Option {i}: <t>{opt}</t>")
        tagged_lines.append(f"Explanation: <t>{q['exp']}</t>")
        tagged_lines.append("---")
    return "\n".join(tagged_lines)

QUIZ_TOPIC_EXTRACTION_PROMPT = """Extract 3 foundational theories or concepts that are essential for understanding the provided educational context.

INSTRUCTIONS:
1. Output exclusively a comma-separated list of topics.
2. Do not include introductory or concluding conversational text.

### CONTEXT
{topic_context}
###"""

QUIZ_GENERATION_PROMPT = """Generate exactly {max_questions} multiple-choice questions based on the provided topics.

INSTRUCTIONS:
1. Limit the Question text (q) to 250 characters maximum.
2. Limit each Option to 100 characters maximum.
3. Limit the Explanation (exp) to 200 characters maximum.
4. Provide exactly 4 options per question.
5. Ensure questions are educational, challenging, and have clear, correct answers.
6. Output exclusively a JSON object matching the expected format below.

EXPECTED FORMAT:
{{
  "questions": [
    {{
      "q": "Question text...",
      "options": ["Option A", "Option B", "Option C", "Option D"],
      "correct": 0,
      "exp": "Explanation..."
    }}
  ]
}}

### TOPICS
{topics}
###
"""
