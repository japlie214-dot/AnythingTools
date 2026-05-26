# tools/draft_editor/__init__.py
"""
Draft Editor Tool Documentation
==============================

Use this tool to deterministically reorder or replace articles within a "Top 10" 
curated batch. It performs SWAP operations to ensure exactly 10 articles remain 
in the list.

Endpoint: /api/tools/draft_editor

Filling Instructions:

${batch_id}: The unique ULID of the news batch to edit. Must be in 'PENDING', 'PARTIAL', or 'FAILED' status.

${operations}: A list of SWAP operations targeting a specific 0-based position (`index_top10`) 
and replacing it with either an integer (internal swap) or string ULID (replacement from inventory).

Schema:
{
  "type": "object",
  "properties": {
    "batch_id": { "type": "string" },
    "operations": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "index_top10": { "type": "integer", "minimum": 0, "maximum": 9 },
          "target_identifier": {}
        },
        "required": ["index_top10", "target_identifier"]
      }
    }
  },
  "required": ["batch_id", "operations"]
}

Developer Notes:
----------------
- Resume Mechanism: This tool does NOT support resumption. Operations are executed as
  atomic transactions. Re-run operations directly if an error occurs.
"""

from .tool import DraftEditorTool

__all__ = ["DraftEditorTool"]
