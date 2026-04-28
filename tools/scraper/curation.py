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

    def _score_curation_quality(self, curated: list[dict], packed_candidates: list[dict], target_count: int) -> dict:
        candidate_ulids = {item["ulid"] for item in packed_candidates}
        
        coverage_ratio = min(len(curated) / target_count, 1.0) if target_count > 0 else 0.0
        
        ulids = [item.get("ulid") for item in curated if "ulid" in item]
        unique_ulids = set(ulids)
        uniqueness_score = len(unique_ulids) / len(ulids) if ulids else 0.0
        
        valid_count = sum(1 for u in ulids if u in candidate_ulids)
        validity_score = valid_count / len(ulids) if ulids else 0.0
        
        conclusion_lengths = [len(str(item.get("conclusion", ""))) for item in curated]
        if len(conclusion_lengths) > 1:
            mean_len = sum(conclusion_lengths) / len(conclusion_lengths)
            variance = sum((l - mean_len) ** 2 for l in conclusion_lengths) / len(conclusion_lengths)
            diversity_score = min(variance / 1000.0, 1.0)
        else:
            diversity_score = 0.0
        
        composite = (
            coverage_ratio * 0.35 +
            uniqueness_score * 0.25 +
            validity_score * 0.30 +
            diversity_score * 0.10
        )
        
        return {
            "composite": round(composite, 4),
            "coverage_ratio": round(coverage_ratio, 4),
            "uniqueness_score": round(uniqueness_score, 4),
            "validity_score": round(validity_score, 4),
            "diversity_score": round(diversity_score, 4),
            "item_count": len(curated),
            "target_count": target_count,
        }

    def curate(self, candidates: list[dict], sync_llm_chat, batch_id: str = None) -> tuple[list[dict], int]:
        """
        Execute validated curation with 3-retry loop and quality scoring.
        """
        from utils.logger.structured import granular_log
        
        log_ctx = {"batch_id": batch_id} if batch_id else {}
        if not candidates:
            return [], 0

        # Phase 0: Strict validation - reject candidates without ulid upfront
        valid_candidates = [c for c in candidates if c.get("ulid")]
        if len(valid_candidates) < len(candidates):
            missing_count = len(candidates) - len(valid_candidates)
            log.dual_log(
                tag="Scraper:Curation:Validation",
                message=f"Filtered out {missing_count} candidates without ulid",
                level="WARNING",
                payload={"original_count": len(candidates), "valid_count": len(valid_candidates)}
            )
        if not valid_candidates:
            log.dual_log(
                tag="Scraper:Curation:Validation",
                message="No valid candidates with ulid found",
                level="ERROR",
                payload={"candidates_sample": [str(c)[:100] for c in candidates[:3]]}
            )
            return [], 0

        # Phase 1: Smart packing
        with granular_log("Scraper:Curation:Pack", valid_count=len(valid_candidates)):
            packed_candidates, target_count = self._pack_context(valid_candidates)
        if target_count == 0:
            return [], 0

        candidate_ulids = {item["ulid"] for item in packed_candidates}
        ulid_to_item = {item["ulid"]: item for item in packed_candidates}
        previous_errors = []
        best_result = None
        best_score = -1.0

        # Phase 2: Validated curation with 3 retries
        with granular_log("Scraper:Curation:LLM", target_count=target_count):
            for attempt in range(1, self.MAX_RETRIES + 1):
                prompt = f"Return a JSON object with key 'top_{target_count}' containing an array of EXACTLY {target_count} unique ULIDs.\n"
                if previous_errors:
                    prompt += f"\nWARNINGS from past attempts:\n"
                    for err in previous_errors:
                        prompt += f"- {err}\n"
                    prompt += "\nDo NOT repeat these mistakes.\n"
                
                prompt += f"\nCandidates:\n{json.dumps(packed_candidates, ensure_ascii=False)}"

                try:
                    log.dual_log(
                        tag="LLM:Azure:Request",
                        message="Sending request to Azure OpenAI (Curation)",
                        payload={**log_ctx, "attempt": attempt}
                    )
                    resp = sync_llm_chat(
                        [{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"}
                    )
                    log.dual_log(
                        tag="LLM:Azure:Response",
                        message="Received response from Azure OpenAI.",
                        payload={**log_ctx, "attempt": attempt}
                    )
                    
                    parsed = json.loads(resp.content or "{}")
                    key = f"top_{target_count}"
                    top_ulids = parsed.get(key) or parsed.get("top_10") or []
                    
                    seen = set()
                    valid_ulids = []
                    for u in top_ulids:
                        if u in candidate_ulids and u not in seen:
                            seen.add(u)
                            valid_ulids.append(u)
                            
                    curated = [ulid_to_item[u] for u in valid_ulids]
                    quality = self._score_curation_quality(curated, packed_candidates, target_count)
                    
                    log.dual_log(
                        tag="Scraper:Curation:Score",
                        message=f"Quality score for attempt {attempt}: {quality['composite']}",
                        payload={**log_ctx, "attempt": attempt, "quality": quality}
                    )
                    
                    if quality["composite"] > best_score:
                        best_score = quality["composite"]
                        best_result = (curated, target_count)
                        
                    if len(valid_ulids) == target_count:
                        log.dual_log(tag="Scraper:Curation:Selected", message=f"Selected attempt {attempt} as final result.", payload={**log_ctx, "attempt": attempt})
                        return curated, target_count
                    else:
                        err_msg = f"Expected {target_count} valid unique items, got {len(valid_ulids)}."
                        previous_errors.append(err_msg)
                        log.dual_log(tag="Scraper:Curation:Partial", message=err_msg, level="WARNING", payload={"attempt": attempt})

                except Exception as e:
                    previous_errors.append(f"Parse error: {e}")

        # Fallback: Retry exhaustion
        log.dual_log(
            tag="Scraper:Curation:Exhausted",
            message="Curation retries exhausted.",
            level="WARNING",
            payload={"errors": previous_errors, "best_score": best_score if best_score >= 0 else None}
        )
        
        if best_result is not None and best_result[0]:
            log.dual_log(tag="Scraper:Curation:Selected", message=f"Selected highest scoring partial result (score: {best_score}).", payload=log_ctx)
            return best_result

        log.dual_log(tag="Scraper:Curation:Selected", message="Selected fallback sequential slice.", payload=log_ctx)
        return packed_candidates[:target_count], target_count
