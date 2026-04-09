# tools/actions/system/drafteditor/prompt.py
# Draft Editor prompts — expected input and output schemas

DRAFT_EDITOR_INSTRUCTIONS = """
Input (JSON):
{
  "batch_id": "<ulid>",
  "operations": [
    {"action": "ADD|REMOVE|SWAP|REORDER", "target_ulid": "...", "replacement_ulid": "...", "new_index": 2}
  ]
}

Output (JSON):
{
  "status": "SUCCESS|ERROR",
  "updated_top_10": [ {"ulid": "...", "title": "...", "url": "..."} ]
}
"""
