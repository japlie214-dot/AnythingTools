# deprecated/tools/research/curator.py
"""
Knowledge Curator Sub-Agent for Research Module.

This module implements a post-research phase that evaluates the generated report
against existing database memories and outputs structured decisions to update
persistent institutional knowledge.
"""

import json
from typing import List, Dict, Any

from clients.llm import get_llm_client, LLMRequest
from database.writer import enqueue_write
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)
from utils.vector_search import generate_embedding
from tools.research.curator_prompts import CURATOR_EXTRACTION_PROMPT


async def curate_report_knowledge(chat_id: int | None, full_report_xml: str) -> str:
    """
    Evaluates the final report to extract durable, timeless facts into LTM.

    This function accepts a chat_id (int or None) and stores long-term memories
    associated with that chat. If chat_id is None, memories are stored with NULL
    chat_id in the database.

    Args:
        chat_id: Integer chat identifier or None
        full_report_xml: Complete XML report from the research pipeline

    Returns:
        JSON string representing the curation results
    """
    llm = get_llm_client(provider_type="azure")
    
    from utils.text_processing import escape_prompt_separators
    prompt = CURATOR_EXTRACTION_PROMPT.format(report=escape_prompt_separators(full_report_xml))

    try:
        response = await llm.complete_chat(
            LLMRequest(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}
            )
        )
        
        # Parse the JSON response
        data = json.loads(response.content)
        memories = data.get("memories", []) if isinstance(data, dict) else data
        
        curated_count = 0
        
        # Validate and store each memory
        for mem in memories:
            if not isinstance(mem, dict):
                continue
                
            decision = mem.get("decision")
            mem_type = mem.get("type", "Knowledge")
            topic = mem.get("topic")
            final_memory = mem.get("final_memory")
            
            # Basic validation
            if decision not in ["New", "Update"]:
                continue
            if mem_type not in ["Knowledge", "Values"]:
                continue
            if not topic or not final_memory:
                continue
            
            # Generate embedding and store in database
            try:
                embedding = await generate_embedding(f"{topic}: {final_memory}")
                enqueue_write(
                    """
                    INSERT INTO long_term_memories (chat_id, topic, memory, embedding, type)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, topic, final_memory, embedding, mem_type)
                )
            except Exception as e:
                log.dual_log(
                    tag="Research:Curator",
                    message=f"Failed to generate embedding, storing without: {e}",
                    level="WARNING",
                )
                # Fallback: store without embedding if generation fails
                enqueue_write(
                    """
                    INSERT INTO long_term_memories (chat_id, topic, memory, type)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chat_id, topic, final_memory, mem_type)
                )
                curated_count += 1
        
        result = {
            "curated_count": curated_count,
            "memories_stored": memories
        }
        
        log.dual_log(
            tag="Research:Curator",
            message=f"Knowledge curation complete: {curated_count} memories stored",
            payload={"curated_count": curated_count},
        )
        
        return json.dumps(result)
        
    except json.JSONDecodeError as e:
        log.dual_log(
            tag="Research:Curator",
            message=f"Knowledge curator received invalid JSON from LLM: {e}",
            level="ERROR",
        )
        return json.dumps({"error": "Invalid JSON from LLM", "curated_count": 0})
        
    except Exception as e:
        log.dual_log(
            tag="Research:Curator",
            message=f"Knowledge curation failed: {e}",
            level="ERROR",
            exc_info=e,
        )
        return json.dumps({"error": str(e), "curated_count": 0})
    