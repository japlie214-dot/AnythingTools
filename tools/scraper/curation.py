# tools/scraper/curation.py
import json
import asyncio
from dataclasses import dataclass, field
from typing import List, Tuple, Any

import config
from utils.logger import get_dual_logger
from utils.logger.structured import granular_log
from tools.scraper.prompts import CURATION_SYS_PROMPT, CURATION_SCHEMA
from clients.llm import get_llm_client, LLMRequest
from utils.logger.tags import (
    SCRAPER_CURATION_CANDIDATES,
    SCRAPER_CURATION_PACK,
    SCRAPER_CURATION_LLM_REQUEST,
    SCRAPER_CURATION_LLM_RESPONSE,
    SCRAPER_CURATION_RESULT
)

log = get_dual_logger(__name__)


@dataclass(frozen=True)
class CurationCandidate:
    ulid: str
    title: str
    conclusion: str
    url: str


@dataclass
class CurationResult:
    curated_list: List[dict]
    target_count: int
    metadata: dict
    fallback_used: bool = False


class ContextPacker:
    @staticmethod
    def pack(candidates: List[CurationCandidate]) -> Tuple[List[CurationCandidate], int, str]:
        budget = int(getattr(config, "LLM_CONTEXT_CHAR_LIMIT", 40000) * 0.8)
        
        sorted_cands = sorted(
            candidates,
            key=lambda x: (-len(str(x.conclusion or "")), str(x.title or ""))
        )
        
        packed = []
        current_len = 0
        mode = "full"

        for c in sorted_cands:
            item_str = json.dumps({"title": c.title, "conclusion": c.conclusion, "ulid": c.ulid}, ensure_ascii=False)
            if current_len + len(item_str) > budget:
                if not packed:  # Ensure at least one makes it through
                    packed.append(c)
                    mode = "truncate"
                break
            packed.append(c)
            current_len += len(item_str)
            
        target_count = min(10, len(packed))
        return packed, target_count, mode


class Top10Curator:
    MAX_RETRIES = 3

    async def curate(self, candidates: List[dict], telemetry: Any, batch_id: str = None) -> CurationResult:
        log_ctx = {"batch_id": batch_id} if batch_id else {}
        if not candidates:
            return CurationResult([], 0, log_ctx, fallback_used=False)

        # Phase 0: Assembly
        # Filter out articles with extremely short summaries to save LLM context
        MIN_SUMMARY_CHARS = 600
        valid_candidates = [
            CurationCandidate(
                ulid=c.get("ulid"), title=c.get("title", ""),
                conclusion=c.get("conclusion", ""), url=c.get("normalized_url", c.get("url", ""))
            ) for c in candidates
            if c.get("ulid") and len(str(c.get("summary", "")).strip()) >= MIN_SUMMARY_CHARS
        ]
        
        log.dual_log(
            tag=SCRAPER_CURATION_CANDIDATES,
            message=f"Assembled {len(valid_candidates)} valid candidates.",
            level="INFO",
            payload={"original_count": len(candidates), "valid_count": len(valid_candidates), **log_ctx}
        )

        if not valid_candidates:
            return CurationResult([], 0, log_ctx, fallback_used=False)

        # Phase 1: Context Packing
        with granular_log(SCRAPER_CURATION_PACK, valid_count=len(valid_candidates)):
            packed_candidates, target_count, content_mode = ContextPacker.pack(valid_candidates)
            log_ctx["content_mode"] = content_mode
            
        if target_count == 0:
            return CurationResult([], 0, log_ctx, fallback_used=False)

        candidate_ulids = {c.ulid for c in packed_candidates}
        ulid_to_item = {c.ulid: {"ulid": c.ulid, "title": c.title, "conclusion": c.conclusion, "normalized_url": c.url} for c in packed_candidates}
        
        # Phase 2: LLM Invocation
        llm = get_llm_client("azure")
        previous_errors = []
        
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await telemetry({"timestamp": "", "tool_name": "scraper", "message": f"Curating top articles (Attempt {attempt}/{self.MAX_RETRIES})...", "status": "RUNNING"})
            except Exception:
                pass

            prompt = f"Candidates:\n{json.dumps([{'ulid': c.ulid, 'title': c.title, 'conclusion': c.conclusion} for c in packed_candidates], ensure_ascii=False)}"
            if previous_errors:
                # Keep only the last error to prevent context blowout doom-loop
                prompt = f"WARNING from past attempt:\n- {previous_errors[-1]}\nDo NOT repeat this mistake.\n\n" + prompt

            call_ctx = {**log_ctx, "phase": "curation", "attempt": attempt, "target_count": target_count}
            
            log.dual_log(
                tag=SCRAPER_CURATION_LLM_REQUEST,
                message=f"Sending curation request (Attempt {attempt})",
                payload={**call_ctx}
            )

            request = LLMRequest(
                messages=[
                    {"role": "system", "content": CURATION_SYS_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_schema", "json_schema": {"name": "curation", "strict": True, "schema": CURATION_SCHEMA}},
                call_context=call_ctx,
                timeout_s=120.0
            )

            try:
                resp = await llm.complete_chat(request)
                
                log.dual_log(
                    tag=SCRAPER_CURATION_LLM_RESPONSE,
                    message=f"Received curation response (Attempt {attempt})",
                    payload={**call_ctx, "finish_reason": resp.finish_reason}
                )
                
                if not resp.content:
                    previous_errors.append("LLM returned empty response")
                    continue
                    
                parsed = json.loads(resp.content)
                top_ulids = parsed.get("top_10", [])
                
                valid_ulids = []
                seen = set()
                for u in top_ulids:
                    if u in candidate_ulids and u not in seen:
                        seen.add(u)
                        valid_ulids.append(u)
                        
                if len(valid_ulids) == target_count:
                    curated = [ulid_to_item[u] for u in valid_ulids]
                    log.dual_log(tag=SCRAPER_CURATION_RESULT, message="Curation successful", level="INFO", payload={**call_ctx, "success": True})
                    return CurationResult(curated, target_count, call_ctx, fallback_used=False)
                else:
                    previous_errors.append(f"Expected {target_count} valid unique ULIDs, got {len(valid_ulids)}.")

            except Exception as e:
                log.dual_log(tag=SCRAPER_CURATION_LLM_RESPONSE, message=f"LLM call failed: {e}", level="ERROR", payload={**call_ctx, "error": str(e)})
                previous_errors.append(f"Exception: {e}")

        # Fallback to Sequential Slice
        log.dual_log(
            tag=SCRAPER_CURATION_RESULT,
            message="Curation retries exhausted. Using sequential fallback.",
            level="WARNING",
            payload={"errors": previous_errors, **log_ctx}
        )
        fallback_curated = [ulid_to_item[c.ulid] for c in packed_candidates[:target_count]]
        return CurationResult(fallback_curated, target_count, log_ctx, fallback_used=True)
