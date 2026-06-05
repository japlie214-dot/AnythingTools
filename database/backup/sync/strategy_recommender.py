# database/backup/sync/strategy_recommender.py
from enum import Enum
from typing import Dict
from dataclasses import dataclass

class RecommendedStrategy(str, Enum):
    NEWEST_WINS = "newest_wins"
    OPERATIONAL_WINS = "operational_wins"
    LOCAL_BACKUP_WINS = "local_backup_wins"
    CLOUD_BACKUP_WINS = "cloud_backup_wins"
    ABORT = "abort"

@dataclass
class SyncRecommendation:
    overall_strategy: RecommendedStrategy
    overall_confidence: float
    per_table: Dict[str, dict]
    reasoning: str
    safe_to_auto_accept: bool

class StrategyRecommender:
    @classmethod
    def recommend(cls, table_metrics: Dict[str, dict], local_enabled: bool, cloud_enabled: bool) -> SyncRecommendation:
        if not table_metrics:
            return SyncRecommendation(RecommendedStrategy.OPERATIONAL_WINS, 1.0, {}, "No tables to sync", True)
            
        all_high_match = True
        scores = {}
        for name, metrics in table_metrics.items():
            total = max(metrics.get("total_rows", 1), 1)
            content_match = len(metrics.get("content_identical", [])) / total if "content_identical" in metrics else 0
            conflicts = len(metrics.get("genuine_conflicts", [])) / total if "genuine_conflicts" in metrics else 0
            op_rows = metrics.get("op_rows", 0)
            bk_rows = metrics.get("bk_rows", 0)
            
            if content_match < 0.99 or conflicts >= 0.01:
                all_high_match = False
                
            if op_rows > 0 and bk_rows == 0:
                scores[RecommendedStrategy.OPERATIONAL_WINS] = scores.get(RecommendedStrategy.OPERATIONAL_WINS, 0) + 1.0
            elif bk_rows > 0 and op_rows == 0:
                scores[RecommendedStrategy.LOCAL_BACKUP_WINS] = scores.get(RecommendedStrategy.LOCAL_BACKUP_WINS, 0) + 0.9
            else:
                scores[RecommendedStrategy.NEWEST_WINS] = scores.get(RecommendedStrategy.NEWEST_WINS, 0) + 0.6
                
        best_strategy = RecommendedStrategy.NEWEST_WINS
        if scores:
            best_strategy = max(scores, key=scores.get)
            
        if best_strategy == RecommendedStrategy.LOCAL_BACKUP_WINS and not local_enabled:
            best_strategy = RecommendedStrategy.CLOUD_BACKUP_WINS
            
        confidence = 0.95 if all_high_match else 0.6
        reasoning = "Near-perfect content match; safe to merge." if all_high_match else "Conflicts detected, HITL required."
        
        return SyncRecommendation(best_strategy, confidence, table_metrics, reasoning, confidence >= 0.85)
