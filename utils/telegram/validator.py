# utils/telegram/validator.py
from typing import Dict, Tuple, List
from utils.logger import get_dual_logger
from utils.metadata_helpers import make_metadata, STEP_VALIDATE

log = get_dual_logger(__name__)

class ArticleValidator:
    def __init__(self, job_id: str | None = None):
        self.job_id = job_id

    def is_valid(self, article: Dict) -> Tuple[bool, str]:
        ulid = article.get("ulid")
        if not ulid or ulid == "None":
            return False, f"Invalid ULID: {ulid!r}"
        title = article.get("title")
        if not title or not isinstance(title, str) or not title.strip():
            return False, f"Missing/empty title for ULID {ulid}"
        return True, ""

    def validate_batch(self, articles: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        from database.job_queue import add_job_item, update_item_status
        import json
        
        valid, skipped = [], []
        for article in articles:
            ok, reason = self.is_valid(article)
            if ok:
                valid.append(article)
            else:
                skipped.append(article)
                ulid = article.get("ulid", "UNKNOWN")
                log.dual_log(tag="Publisher:Validate:Skip", message=f"Skipping invalid item: {reason}", level="WARNING")
                if self.job_id:
                    meta = make_metadata(STEP_VALIDATE, ulid, error=f"Skipped: {reason}", is_top10=article.get("_is_top10", False))
                    add_job_item(self.job_id, meta, json.dumps(article, ensure_ascii=False))
                    update_item_status(self.job_id, meta, "FAILED", "{}")
        return valid, skipped
