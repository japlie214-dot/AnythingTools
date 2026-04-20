# tools/publisher/prompt.py
PUBLISHER_PROMPT = """
Input: {"batch_id": "<ulid>", "target_channels": ["@channel1"]}
Output (JSON): {"status": "COMPLETED|PARTIAL", "messages_sent": 0}
"""

TRANSLATION_PROMPT = """
Translate the following JSON array of articles into Bahasa Indonesia.

CRITICAL FORMATTING RULES:
- The output will be parsed using Telegram's MarkdownV2.
- Use *italic* or _italic_ for emphasis.
- Do NOT use HTML tags. 
- Replace the heading "Conclusion:" entirely with "Kesimpulan:"
- Preserve all structural line breaks.

Return EXACTLY a JSON object with this structure:
{
  "translations": [
    {
      "ulid": "<original ulid>",
      "translated_title": "<title in Bahasa Indonesia>",
      "translated_summary": "<summary in Bahasa Indonesia>",
      "translated_conclusion": "<conclusion in Bahasa Indonesia (starting with Kesimpulan: instead of Conclusion:)>"
    }
  ]
}

Input articles:
{input_json}
"""
