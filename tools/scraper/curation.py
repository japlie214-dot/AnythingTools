# tools/scraper/curation.py
"""
Smart Context Packing and Validated Curation engine.
Implements knapsack-style greedy packing and validated LLM curation with retry logic.
"""

import json
import config
from utils.logger import get_dual_logger
from tools.scraper.prompts import CURATION_SYS_PROMPT

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

    def _pack_context(self, candidates: list[dict]) -> tuple[list[dict], int, str]:
        """
        Pack articles smartly up to 80% of the LLM context limit using a tiered approach.
        """
        budget = int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 40000) * 0.8)
        
        sorted_cands = sorted(
            candidates,
            key=lambda x: (-len(str(x.get("conclusion") or "")), str(x.get("title") or ""))
        )
        
        def _calc_len(mode):
            total = 0
            for c in sorted_cands:
                if mode == "full": total += len(json.dumps({"title": c.get("title"), "conclusion": c.get("conclusion"), "ulid": c.get("ulid")}))
                elif mode == "lite": total += len(json.dumps({"conclusion": c.get("conclusion"), "ulid": c.get("ulid")}))
                else: total += len(json.dumps({"title": c.get("title"), "ulid": c.get("ulid")}))
            return total

        mode = "full"
        if _calc_len("full") > budget:
            mode = "lite"
            if _calc_len("lite") > budget:
                mode = "liter"
                if _calc_len("liter") > budget:
                    mode = "truncate"
        
        packed = []
        current_len = 0
        
        for c in sorted_cands:
            if mode == "full": item = {"title": c.get("title"), "conclusion": c.get("conclusion"), "ulid": c.get("ulid")}
            elif mode == "lite": item = {"conclusion": c.get("conclusion"), "ulid": c.get("ulid")}
            else: item = {"title": c.get("title"), "ulid": c.get("ulid")}
            
            item_str = json.dumps(item, ensure_ascii=False)
            if mode == "truncate" and current_len + len(item_str) > budget:
                break
                
            packed.append(item)
            current_len += len(item_str)
            
        target_count = min(10, len(packed))
        
        if target_count < 10 and len(candidates) >= 10:
            log.dual_log(
                tag="Scraper:Curation:BudgetLimit",
                message=f"Context budget forced dynamic reduction to Top {target_count}",
                level="WARNING",
                payload={"budget": budget, "used": current_len, "available": len(candidates), "count": target_count, "mode": mode}
            )
            
        return packed, target_count, mode

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
            packed_candidates, target_count, content_mode = self._pack_context(valid_candidates)
            log_ctx["content_mode"] = content_mode
            
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
                
                call_ctx = {
                    **log_ctx,
                    "phase": "curation",
                    "attempt": attempt,
                    "target_count": target_count,
                    "candidate_count": len(packed_candidates)
                }

                try:
                    resp = sync_llm_chat(
                        messages=[
                            {"role": "system", "content": CURATION_SYS_PROMPT},
                            {"role": "user", "content": prompt}
                        ],
                        response_format={"type": "json_object"},
                        call_context=call_ctx
                    )
                    
                    if not resp.content or not resp.content.strip():
                        log.dual_log(
                            tag="Scraper:Curation:EmptyResponse",
                            message=f"LLM returned empty response on attempt {attempt}",
                            level="WARNING",
                            payload={**call_ctx, "finish_reason": getattr(resp, 'finish_reason', None)}
                        )
                        previous_errors.append("LLM returned empty response")
                        continue
                        
                    parsed = json.loads(resp.content)
                    key = f"top_{target_count}"
                    top_ulids = parsed.get(key) or parsed.get("top_10") or []
                    
                    if top_ulids and not parsed.get(key):
                        log.dual_log(
                            tag="Scraper:Curation:KeyMismatch",
                            message=f"LLM returned unexpected JSON key on attempt {attempt}",
                            level="WARNING",
                            payload={**call_ctx, "expected_key": key, "actual_keys": list(parsed.keys())}
                        )
                    
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

                except TimeoutError as te:
                    log.dual_log(
                        tag="Scraper:Curation:Timeout",
                        message=f"LLM call timed out on attempt {attempt}",
                        level="ERROR",
                        payload={**call_ctx, "error": str(te)},
                        exc_info=te
                    )
                    previous_errors.append(f"TimeoutError: {te}")
                except json.JSONDecodeError as jde:
                    _resp_preview = (resp.content or "")[:500] if 'resp' in locals() else "N/A"
                    log.dual_log(
                        tag="Scraper:Curation:ParseError",
                        message=f"JSON decode failed on attempt {attempt}",
                        level="ERROR",
                        payload={**call_ctx, "error": str(jde), "error_type": type(jde).__name__, "response_preview": _resp_preview},
                        exc_info=jde
                    )
                    previous_errors.append(f"JSONDecodeError: {jde}")
                except Exception as e:
                    log.dual_log(tag="Scraper:Curation:Error", message=f"Unexpected error: {e}", level="ERROR", payload={**call_ctx, "error": str(e)})
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
