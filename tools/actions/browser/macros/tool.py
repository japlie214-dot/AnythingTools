# tools/actions/browser/macros/tool.py
from __future__ import annotations
import asyncio
import json
from typing import Any

import config
from database.connection import DatabaseManager
from database.writer import enqueue_write
from tools.base import BaseTool
from tools.registry import REGISTRY
from utils.browser_daemon import append_action_log
from utils.id_generator import ULID
from utils.prompt_cache import mark_macros_dirty


class MacroSaveTool(BaseTool):
    from bot.core.constants import TOOL_BROWSER_MACRO_SAVE
    name = TOOL_BROWSER_MACRO_SAVE

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        name       = args.get("name", "").strip()
        steps_json = args.get("steps_json", "").strip()
        if not name:
            return "Error: 'name' is required."
        if not steps_json:
            return "Error: 'steps_json' is required."
        # Validate that steps_json is parseable before persisting.
        try:
            parsed = json.loads(steps_json)
            if not isinstance(parsed, list):
                return "Error: steps_json must be a JSON array."
        except json.JSONDecodeError as exc:
            return f"Error: steps_json is not valid JSON — {exc}"

        conn = DatabaseManager.get_read_connection()
        count = conn.execute("SELECT COUNT(*) FROM browser_macros").fetchone()[0]
        if count >= 10:
            return "Error: macro limit of 10 reached. Delete a macro before saving a new one."

        mac_id = ULID.generate()
        enqueue_write(
            "INSERT INTO browser_macros (id, name, description, steps_json) VALUES (?, ?, ?, ?)",
            (mac_id, name, args.get("description", ""), steps_json),
        )
        mark_macros_dirty()
        return f"Macro '{name}' saved with ID {mac_id}."


class MacroEditTool(BaseTool):
    from bot.core.constants import TOOL_BROWSER_MACRO_EDIT
    name = TOOL_BROWSER_MACRO_EDIT

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Apply index-based step REPLACEMENT deltas to a macro's steps_json.
        Each delta: {"index": int, "step": {tool: str, args: dict}}.
        Step deletion is intentionally unsupported: pop() shifts subsequent indices,
        making multi-delta calls non-deterministic. Reconstruct via delete + save."""
        mac_id = args.get("macro_id", "").strip()
        deltas = args.get("deltas", [])
        if not mac_id:
            return "Error: 'macro_id' is required."
        if not isinstance(deltas, list) or not deltas:
            return "Error: 'deltas' must be a non-empty array."

        conn = DatabaseManager.get_read_connection()
        row  = conn.execute(
            "SELECT steps_json FROM browser_macros WHERE id = ?", (mac_id,)
        ).fetchone()
        if not row:
            return f"Error: macro '{mac_id}' not found."

        steps = json.loads(row[0])
        errors: list[str] = []
        for delta in deltas:
            idx      = delta.get("index")
            new_step = delta.get("step")
            if idx is None or new_step is None:
                errors.append(f"Delta missing 'index' or 'step': {delta}")
                continue
            if not (0 <= idx < len(steps)):
                errors.append(f"Index {idx} out of range (0–{len(steps) - 1}).")
                continue
            steps[idx] = new_step

        enqueue_write(
            "UPDATE browser_macros SET steps_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(steps), mac_id),
        )
        mark_macros_dirty()
        result = f"Macro '{mac_id}' updated ({len(steps)} steps)."
        if errors:
            result += "\nWarnings: " + "; ".join(errors)
        return result


class MacroDeleteTool(BaseTool):
    from bot.core.constants import TOOL_BROWSER_MACRO_DELETE
    name = TOOL_BROWSER_MACRO_DELETE

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        mac_id = args.get("macro_id", "").strip()
        if not mac_id:
            return "Error: 'macro_id' is required."
        enqueue_write("DELETE FROM browser_macros WHERE id = ?", (mac_id,))
        mark_macros_dirty()
        return f"Macro '{mac_id}' deleted."


class MacroExecuteTool(BaseTool):
    from bot.core.constants import TOOL_BROWSER_MACRO_EXECUTE
    name = TOOL_BROWSER_MACRO_EXECUTE

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        mac_id = args.get("macro_id", "").strip()
        if not mac_id:
            return "Error: 'macro_id' is required."

        conn = DatabaseManager.get_read_connection()
        row  = conn.execute(
            "SELECT name, steps_json FROM browser_macros WHERE id = ?", (mac_id,)
        ).fetchone()
        if not row:
            return f"Error: macro '{mac_id}' not found."

        steps    = json.loads(row[1])
        mac_name = row[0]
        summary: list[str] = []

        for i, step in enumerate(steps):
            tool_name = step.get("tool", "")
            tool_args = step.get("args", {})
            tool = REGISTRY.get(tool_name)
            if not tool:
                return f"Halted at step {i}: unknown tool '{tool_name}'."

            try:
                result = await asyncio.wait_for(
                    tool.execute(tool_args, telemetry, **kwargs),
                    timeout=config.MACRO_STEP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return (
                    f"Halted at step {i} ({tool_name}): "
                    f"TIMEOUT after {config.MACRO_STEP_TIMEOUT_SECONDS}s."
                )
            except Exception as exc:
                return f"Halted at step {i} ({tool_name}): Exception — {exc}"

            if not result.success:
                return f"Halted at step {i} ({tool_name}): {result.output}"

            append_action_log({
                "tool_name":    f"macro:{mac_name}:step{i}:{tool_name}",
                "args_summary": {str(k): str(v)[:200] for k, v in tool_args.items()},
                "outcome":      "SUCCESS",
            })
            summary.append(f"Step {i} ({tool_name}): OK")

        return "\n".join(summary) or "Macro completed (0 steps)."
    