# tools/drafteditor/tool.py
import json
import os
import sqlite3
import tempfile
from typing import Any

from database.connection import DatabaseManager
from database.writer import enqueue_write
from tools.base import BaseTool, TelemetryCallback
from utils.logger import get_dual_logger

log = get_dual_logger(__name__)


class DraftEditorTool(BaseTool):
    name = "draft_editor"

    async def run(
        self, args: dict[str, Any], telemetry: TelemetryCallback, **kwargs
    ) -> str:
        batch_id   = args.get("batch_id")
        operations = args.get("operations", [])

        if not batch_id or not operations:
            return "Error: batch_id and operations are required."

        await telemetry(self.status("Loading draft for editing...", "RUNNING"))

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row

        batch_row = conn.execute(
            "SELECT curated_json_path FROM broadcast_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()

        if not batch_row:
            return f"Error: No broadcast batch found for ID '{batch_id}'."

        curated_json_path = batch_row["curated_json_path"]

        if not os.path.exists(curated_json_path):
            return f"Error: Curated JSON file not found at '{curated_json_path}'."

        with open(curated_json_path, "r", encoding="utf-8") as fh:
            top_10: list[dict] = json.load(fh)

        log_output: list[str] = []

        for op in operations:
            action           = op.get("action")
            target_ulid      = op.get("target_ulid")
            replacement_ulid = op.get("replacement_ulid")
            new_index        = op.get("new_index")

            if action == "REMOVE":
                before = len(top_10)
                top_10 = [item for item in top_10 if item["ulid"] != target_ulid]
                if len(top_10) < before:
                    log_output.append(f"OK  REMOVE  {target_ulid}")
                else:
                    log_output.append(
                        f"SKIP REMOVE  {target_ulid} — not found in list."
                    )

            elif action == "SWAP":
                if not target_ulid or not replacement_ulid:
                    log_output.append(
                        "SKIP SWAP — missing target_ulid or replacement_ulid."
                    )
                    continue

                # scraped_articles.id is the ULID TEXT PRIMARY KEY (not the
                # integer vec_rowid); querying by id with replacement_ulid is
                # the correct canonical lookup per GOLDEN RULE 2.
                rep_row = conn.execute(
                    "SELECT id, normalized_url, title, conclusion "
                    "FROM scraped_articles WHERE id = ?",
                    (replacement_ulid,),
                ).fetchone()

                if not rep_row:
                    log_output.append(
                        f"SKIP SWAP  replacement_ulid={replacement_ulid} "
                        f"not found in scraped_articles."
                    )
                    continue

                new_item = {
                    "ulid":           replacement_ulid,
                    "normalized_url": rep_row["normalized_url"],
                    "title":          rep_row["title"],
                    "conclusion":     rep_row["conclusion"],
                }

                swapped = False
                for i, item in enumerate(top_10):
                    if item["ulid"] == target_ulid:
                        top_10[i] = new_item
                        swapped = True
                        break

                if swapped:
                    log_output.append(
                        f"OK  SWAP  {target_ulid} → {replacement_ulid}"
                    )
                else:
                    log_output.append(
                        f"SKIP SWAP  target_ulid={target_ulid} not found in list."
                    )

            elif action == "ADD":
                if not replacement_ulid:
                    log_output.append("SKIP ADD — missing replacement_ulid.")
                    continue

                if len(top_10) >= 10:
                    log_output.append(
                        f"SKIP ADD  {replacement_ulid} — list already has 10 items."
                    )
                    continue

                rep_row = conn.execute(
                    "SELECT id, normalized_url, title, conclusion "
                    "FROM scraped_articles WHERE id = ?",
                    (replacement_ulid,),
                ).fetchone()

                if not rep_row:
                    log_output.append(
                        f"SKIP ADD  {replacement_ulid} — not found in scraped_articles."
                    )
                    continue

                top_10.append({
                    "ulid":           replacement_ulid,
                    "normalized_url": rep_row["normalized_url"],
                    "title":          rep_row["title"],
                    "conclusion":     rep_row["conclusion"],
                })
                log_output.append(f"OK  ADD  {replacement_ulid}")

            elif action == "REORDER":
                if new_index is None or not target_ulid:
                    log_output.append(
                        "SKIP REORDER — missing target_ulid or new_index."
                    )
                    continue

                item_to_move = None
                for i, item in enumerate(top_10):
                    if item["ulid"] == target_ulid:
                        item_to_move = top_10.pop(i)
                        break

                if item_to_move:
                    clamped = max(0, min(new_index, len(top_10)))
                    top_10.insert(clamped, item_to_move)
                    log_output.append(
                        f"OK  REORDER  {target_ulid} → index {clamped}"
                    )
                else:
                    log_output.append(
                        f"SKIP REORDER  {target_ulid} — not found in list."
                    )

        # Hard cap: list must never exceed 10 items.
        top_10 = top_10[:10]

        # Atomic write: temp file in the same directory guarantees os.replace()
        # is an atomic rename on all POSIX and Windows NTFS filesystems.
        dir_name = os.path.dirname(curated_json_path)
        with tempfile.NamedTemporaryFile(
            "w", dir=dir_name, delete=False, suffix=".tmp", encoding="utf-8"
        ) as _tf:
            json.dump(top_10, _tf, indent=2, ensure_ascii=False)
            _tmp_name = _tf.name
        os.replace(_tmp_name, curated_json_path)

        enqueue_write(
            "UPDATE broadcast_batches SET updated_at = CURRENT_TIMESTAMP "
            "WHERE batch_id = ?",
            (batch_id,),
        )

        await telemetry(self.status("Draft updated.", "SUCCESS"))

        formatted = "\n".join(
            f"{i+1}. {item['title']}  [ulid: {item['ulid']}]"
            for i, item in enumerate(top_10)
        )
        ops_log = "\n".join(log_output) or "(no operations logged)"

        return (
            f"DraftEditor complete for batch_id={batch_id}.\n\n"
            f"Operations Log:\n{ops_log}\n\n"
            f"Updated Top {len(top_10)} List:\n{formatted}"
        )
