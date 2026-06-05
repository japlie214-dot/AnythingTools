# database/backup/sync/smart_recommender.py
from dataclasses import dataclass
from typing import Dict

@dataclass
class TableBreakdown:
    table_name: str
    op_only: int
    bk_only: int
    identical: int
    conflicts: int
    op_newer: int
    bk_newer: int
    timestamp_drift: int

    @property
    def conflict_rate(self) -> float:
        total = self.op_only + self.bk_only + self.identical + self.conflicts
        return (self.conflicts / total * 100) if total > 0 else 0.0

@dataclass
class Recommendation:
    strategy: str
    confidence: float
    reasoning: str
    per_table: Dict[str, TableBreakdown]
    outcomes: Dict[str, Dict[str, int]]

class SmartRecommender:
    def recommend(self, metrics: dict) -> Recommendation:
        tables = metrics.get('tables', {})
        per_table = self._compute_breakdowns(tables)
        outcomes = self._compute_outcomes(per_table)

        total_conflicts = sum(t.conflicts for t in per_table.values())
        total_bk_only = sum(t.bk_only for t in per_table.values())
        total_rows = sum(t.op_only + t.bk_only + t.identical + t.conflicts for t in per_table.values())
        overall_conflict_rate = (total_conflicts / total_rows * 100) if total_rows > 0 else 0.0

        if total_conflicts == 0 and total_bk_only == 0:
            strategy, confidence, reasoning = "operational_wins", 0.95, "No conflicts and no backup-only entries."
        elif total_conflicts == 0:
            strategy, confidence, reasoning = "newest_overall_wins", 0.90, "Safe bidirectional merge with zero conflict resolution needed."
        elif overall_conflict_rate < 5.0:
            strategy, confidence, reasoning = "newest_overall_wins", 0.85, f"Low conflict rate ({overall_conflict_rate:.1f}%). Safe to auto-resolve by timestamp."
        elif overall_conflict_rate < 20.0:
            strategy, confidence, reasoning = "newest_overall_wins", 0.60, f"Moderate conflict rate ({overall_conflict_rate:.1f}%). Auto-resolve by timestamp, but review recommended."
        else:
            strategy, confidence, reasoning = "abort", 0.90, f"High conflict rate ({overall_conflict_rate:.1f}%). Manual review strongly recommended."

        return Recommendation(strategy, confidence, reasoning, per_table, outcomes)

    def _compute_breakdowns(self, tables: dict) -> Dict[str, TableBreakdown]:
        breakdowns = {}
        for table_name, data in tables.items():
            conflicts = data.get('genuine_conflicts', [])
            drift = data.get('timestamp_drift', [])
            breakdowns[table_name] = TableBreakdown(
                table_name=table_name,
                op_only=len(data.get('op_only', [])),
                bk_only=len(data.get('bk_only', [])),
                identical=len(data.get('content_identical', [])),
                conflicts=len(conflicts),
                op_newer=sum(1 for d in drift if d.get('op_ts', '') > d.get('bk_ts', '')),
                bk_newer=sum(1 for d in drift if d.get('bk_ts', '') > d.get('op_ts', '')),
                timestamp_drift=len(drift)
            )
        return breakdowns

    def _compute_outcomes(self, per_table: Dict[str, TableBreakdown]) -> Dict[str, Dict[str, int]]:
        outcomes = {}
        for strategy in ["operational_wins", "local_backup_wins", "cloud_backup_wins", "newest_overall_wins"]:
            total = {"op_to_bk": 0, "bk_to_op": 0, "op_deletes": 0, "bk_deletes": 0, "conflict_op_wins": 0, "conflict_bk_wins": 0}
            for tbl in per_table.values():
                if strategy == "operational_wins":
                    total["op_to_bk"] += tbl.op_only
                    total["bk_deletes"] += tbl.bk_only
                    total["conflict_op_wins"] += tbl.conflicts
                elif strategy in ("local_backup_wins", "cloud_backup_wins"):
                    total["bk_to_op"] += tbl.bk_only
                    total["op_deletes"] += tbl.op_only
                    total["conflict_bk_wins"] += tbl.conflicts
                elif strategy == "newest_overall_wins":
                    total["op_to_bk"] += tbl.op_only
                    total["bk_to_op"] += tbl.bk_only
                    total["conflict_op_wins"] += tbl.op_newer + max(0, tbl.conflicts - tbl.op_newer - tbl.bk_newer)
                    total["conflict_bk_wins"] += tbl.bk_newer
            outcomes[strategy] = total
        return outcomes

    def format_outcomes_display(self, rec: Recommendation) -> str:
        lines = ["=== PER-TABLE BREAKDOWN ==="]
        for tbl_name, tbl in sorted(rec.per_table.items()):
            lines.append(f"  {tbl_name}: op_only={tbl.op_only} bk_only={tbl.bk_only} identical={tbl.identical} conflicts={tbl.conflicts}")
        
        lines.append("\n=== STRATEGY OUTCOMES PREVIEW ===")
        for strategy_key, outcome in rec.outcomes.items():
            if any(v > 0 for v in outcome.values()):
                lines.append(f"  {strategy_key}: {outcome}")
        return "\n".join(lines)
