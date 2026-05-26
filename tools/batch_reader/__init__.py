# tools/batch_reader/__init__.py
"""
Batch Reader Tool Documentation
==============================

Use this tool to perform hybrid semantic search (vector + keyword) within a 
specific news batch to find articles matching a natural language query. 

Endpoint: /api/tools/batch_reader

Filling Instructions:

${batch_id}: The unique ULID of the news batch to search within.

${query}: Natural language search query describing the information you're looking for.

${limit}: (Optional) Maximum number of results to return. Range: 1-50. Defaults to 5.

Schema:
{
  "type": "object",
  "properties": {
    "batch_id": {
      "type": "string",
      "description": "REQUIRED: The batch ID to query."
    },
    "query": {
      "type": "string",
      "description": "REQUIRED: Semantic search query."
    },
    "limit": {
      "type": "integer",
      "description": "OPTIONAL: Maximum results to return (1-50)."
    }
  },
  "required": ["batch_id", "query"]
}

Developer Notes:
----------------
- Resume Mechanism: This tool does NOT support resumption. The batch_reader performs 
  stateless search operations. If a search fails, re-run with the same or modified query.
- Idempotency: All queries are strictly read-only and safe to rerun blindly.
"""

from .tool import BatchReaderTool

__all__ = ["BatchReaderTool"]
