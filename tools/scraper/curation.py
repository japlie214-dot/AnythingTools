# tools/scraper/curation.py
"""
Smart Context Packing and Validated Curation engine.
Implements knapsack-style greedy packing and validated LLM curation with retry logic.
"""

import json
import config
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class Top10Curator:
    """
    Smart context packer and validated curation engine.
    
    Features:
    - Knapsack-style greedy packing without article slicing
    - Dynamic target count based on 80% context budget
    - 3-retry validation loop with error context
    - Fallback to sequential slice on retry exhaustion
    """
    
    MAX_RETRIES = 3

    def _pack_context(self, candidates: list[dict]) -> tuple[list[dict], int]:
        """
        Pack articles smartly up to 80% of the LLM context limit.
        
        Algorithm:
        1. Sort by conclusion length (descending) then title (ascending)
        2. Greedily add whole articles until budget reached
        3. Calculate dynamic target count
        
        Args:
            candidates: List of article dictionaries with 'ulid', 'title', 'conclusion'
            
        Returns:
            tuple: (packed_articles, target_count)
        """
        # Calculate budget (80% of context limit)
        budget = int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 40000) * 0.8)
        
        # Tie-breaker: Conclusion length DESC, then Title ASC
        sorted_cands = sorted(
            candidates,
            key=lambda x: (-len(str(x.get("conclusion") or "")), str(x.get("title") or ""))
        )
        
        packed = []
        current_len = 0
        
        # Greedily pack whole articles without slicing
        for art in sorted_cands:
            art_str = json.dumps(art, ensure_ascii=False)
            if current_len + len(art_str) <= budget:
                packed.append(art)
                current_len += len(art_str)
            else:
                # Cannot fit this article, and all following are smaller
                break
        
        # Dynamic target count: min of 10 or packed count
        target_count = min(10, len(packed))
        
        if target_count < 10 and len(candidates) >= 10:
            log.dual_log(
                tag="Scraper:Curation",
                message=f"Context budget forced dynamic reduction to Top {target_count}",
                level="WARNING",
                payload={
                    "budget": budget,
                    "used": current_len,
                    "available": len(candidates),
                    "count": target_count
                }
            )
        
        return packed, target_count

    def curate(self, candidates: list[dict], sync_llm_chat) -> tuple[list[dict], int]:
        """
        Execute validated curation with 3-retry loop.
        
        Validation rules:
        - Exactly target_count items
        - Zero duplicate ULIDs
        - Valid candidate ULIDs only
        
        Fallback: On retry exhaustion, return sequential slice of packed articles.
        
        Args:
            candidates: List of article dictionaries
            sync_llm_chat: Synchronous LLM chat function
            
        Returns:
            tuple: (curated_articles, target_count)
        """
        if not candidates:
            return [], 0

        # Phase 1: Smart packing
        packed_candidates, target_count = self._pack_context(candidates)
        if target_count == 0:
            return [], 0

        # Prepare validation data
        candidate_ulids = {item["ulid"] for item in packed_candidates}
        ulid_to_item = {item["ulid"]: item for item in packed_candidates}
        previous_errors = []

        # Phase 2: Validated curation with 3 retries
        for attempt in range(1, self.MAX_RETRIES + 1):
            # Build prompt with error context
            prompt = f"Return a JSON object with key 'top_{target_count}' containing an array of EXACTLY {target_count} unique ULIDs.\n"
            
            if previous_errors:
                prompt += f"\nWARNINGS from past attempts:\n"
                for err in previous_errors:
                    prompt += f"- {err}\n"
                prompt += "\nDo NOT repeat these mistakes.\n"
            
            prompt += f"\nCandidates:\n{json.dumps(packed_candidates, ensure_ascii=False)}"

            try:
                # Call LLM
                resp = sync_llm_chat(
                    [{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}
                )
                
                # Parse response (handle dynamic key)
                parsed = json.loads(resp.content or "{}")
                key = f"top_{target_count}"
                
                # Support both dynamic key and fallback top_10
                top_ulids = parsed.get(key) or parsed.get("top_10") or []
                
                # Validation
                if len(top_ulids) != target_count:
                    previous_errors.append(f"Expected {target_count} items, got {len(top_ulids)}")
                    continue
                
                if len(set(top_ulids)) != len(top_ulids):
                    previous_errors.append("Output contains duplicate ULIDs.")
                    continue
                
                invalid = [u for u in top_ulids if u not in candidate_ulids]
                if invalid:
                    previous_errors.append(f"Invalid ULIDs provided: {invalid}")
                    continue
                
                # Success - compose curated list
                curated = [ulid_to_item[u] for u in top_ulids]
                return curated, target_count

            except Exception as e:
                previous_errors.append(f"Parse error: {e}")

        # Fallback: Retry exhaustion, use sequential slice
        log.dual_log(
            tag="Scraper:Curation",
            message="Curation retries exhausted, using fallback slice.",
            level="WARNING",
            payload={"errors": previous_errors}
        )
        return packed_candidates[:target_count], target_count
