# tools/actions/system/skills/tool.py
from __future__ import annotations
import sqlite3
from typing import Any

from database.connection import DatabaseManager
from database.writer import enqueue_write
from tools.base import BaseTool


class SkillListTool(BaseTool):
    from bot.core.constants import TOOL_SYSTEM_SKILL_LIST
    name = TOOL_SYSTEM_SKILL_LIST

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, title FROM ai_skills ORDER BY id").fetchall()
        if not rows:
            return "No skills saved."
        return "\n".join(f"{r['id']}: {r['title']}" for r in rows)


class SkillReadTool(BaseTool):
    from bot.core.constants import TOOL_SYSTEM_SKILL_READ
    name = TOOL_SYSTEM_SKILL_READ

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        ids = args.get("ids", [])
        if not ids:
            return "Error: 'ids' array is required."
        ids = ids[:5]  # hard cap at 5 per call

        conn = DatabaseManager.get_read_connection()
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id, title, content FROM ai_skills WHERE id IN ({placeholders})", ids
        ).fetchall()
        if not rows:
            return "No skills found for the given IDs."

        blocks: list[str] = []
        for r in rows:
            lines = r["content"].split("\n")
            numbered = "\n".join(f"{i + 1}\t{line}" for i, line in enumerate(lines))
            blocks.append(f"--- SKILL {r['id']}: {r['title']} ---\n{numbered}")
        return "\n\n".join(blocks)


class SkillCrudTool(BaseTool):
    from bot.core.constants import TOOL_SYSTEM_SKILL_CRUD
    name = TOOL_SYSTEM_SKILL_CRUD

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        action = args.get("action", "").upper()

        if action == "CREATE":
            title   = args.get("title", "").strip()
            content = args.get("content", "")
            if not title:
                return "Error: 'title' is required for CREATE."
            enqueue_write(
                "INSERT INTO ai_skills (title, content) VALUES (?, ?)",
                (title, content),
            )
            return f"Skill '{title}' created."

        if action == "DELETE":
            skill_id = args.get("id")
            if skill_id is None:
                return "Error: 'id' is required for DELETE."
            enqueue_write("DELETE FROM ai_skills WHERE id = ?", (skill_id,))
            return f"Skill {skill_id} deleted."

        if action == "REPLACE_LINES":
            skill_id    = args.get("id")
            start_line  = args.get("start_line")
            end_line    = args.get("end_line")
            new_content = args.get("content", "")
            if any(v is None for v in (skill_id, start_line, end_line)):
                return "Error: 'id', 'start_line', and 'end_line' are required for REPLACE_LINES."

            conn = DatabaseManager.get_read_connection()
            row  = conn.execute(
                "SELECT content FROM ai_skills WHERE id = ?", (skill_id,)
            ).fetchone()
            if not row:
                return f"Error: skill {skill_id} not found."

            lines     = row[0].split("\n")
            new_lines = new_content.split("\n")
            # start_line and end_line are 1-based inclusive; convert to 0-based slice.
            lines[start_line - 1 : end_line] = new_lines
            updated = "\n".join(lines)

            enqueue_write(
                "UPDATE ai_skills SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (updated, skill_id),
            )
            numbered = "\n".join(f"{i + 1}\t{ln}" for i, ln in enumerate(lines))
            return f"Skill {skill_id} updated.\n{numbered}"

        return f"Error: unknown action '{action}'. Use CREATE | DELETE | REPLACE_LINES."
    