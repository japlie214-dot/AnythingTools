# tools/publisher/__init__.py
"""
Publisher Tool Documentation
============================

Use this tool to deterministically translate and publish curated articles from a 
broadcast batch to Telegram channels (archive posts and briefing posts). 

Endpoint: /api/tools/publisher

Filling Instructions:

${batch_id}: The unique ULID of the broadcast batch to publish. 
    Must correspond to a batch in 'PENDING', 'PARTIAL', or 'PUBLISHING' status.

${reset}: (Optional) Set to true to force a full reset of publish status, re-publishing
    all articles from scratch. Defaults to false.

${finalize}: (Optional) Set to true to mark a PARTIAL batch as COMPLETED without 
    re-publishing. Skips all unprocessed items. Defaults to false.

Schema:
{
  "type": "object",
  "properties": {
    "batch_id": {
      "type": "string",
      "description": "REQUIRED: The unique ULID of the news batch to publish."
    },
    "reset": {
      "type": "boolean",
      "description": "OPTIONAL: Force full reset and re-publish all articles."
    },
    "finalize": {
      "type": "boolean",
      "description": "OPTIONAL: Mark partial batch as completed without re-publishing."
    }
  },
  "required": ["batch_id"]
}

Developer Notes:
----------------
- Resume Mechanism: Supported via /api/jobs/${job_id}/resume. When a publishing 
  job is interrupted (e.g., by a rate limit), call the resume endpoint to continue 
  from the last checkpoint. 
- Producer-Consumer: The pipeline acts dynamically and natively skips items already
  published by checking `broadcast_details` states. Do not inject `resume` args natively.
"""

from .tool import PublisherTool

__all__ = ["PublisherTool"]
