# tools/publisher/prompt.py
PUBLISHER_PROMPT = """
Input: {"batch_id": "<ulid>", "target_channels": ["@channel1"]}
Output (JSON): {"status": "COMPLETED|PARTIAL", "messages_sent": 0}
"""

TRANSLATION_PROMPT = r"""
Translate the following JSON array of articles into Bahasa Indonesia.

CRITICAL FORMATTING RULES:
- The output will be sent via Telegram MarkdownV2 parser.
- Use *bold* for titles and _italic_ for emphasis ONLY.
- Do NOT use HTML tags.
- Replace "Conclusion:" entirely with "Kesimpulan:"
- Preserve all structural line breaks.
- Do NOT use these chars outside Markdown: _ * [ ] ( ) ~ ` > # + - = | { } . !.
- If needed in plain text, escape with backslash (e.g. \. \! \-)
- For URLs, use [text](url) format. Do NOT leave bare URLs.
- Avoid nested formatting and multi-line bold/italic blocks.

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
