"""Module containing logger agent prompts and utilities."""
import json
from typing import List

ERROR_HEADER: str = "### ⚠️ Tool Execution Failure"

LOGGER_AGENT_SYSTEM_PROMPT: str = (
    "You are a diagnostic engineering agent. Your task is to analyze a "
    "tool execution failure and output a structured diagnosis.\n\n"
    "INSTRUCTIONS:\n"
    "1. Perform a technical root-cause analysis based on the provided traceback, logs, and source code.\n"
    "2. Identify the failing function exactly as filename.py::function_name.\n"
    "3. Focus solely on diagnosing the root cause; omit any code fixes, patches, or corrected code suggestions.\n"
    "4. Respond with a JSON object containing exactly two fields:\n"
    f'   - "user_message": MUST begin with the exact header line: "{ERROR_HEADER}" followed by a newline, then a 2 to 3 sentence non-technical explanation suitable for an end-user.\n'
    "   - \"developer_diagnosis\": MUST contain the identified fault location, the exception type and message, the causal chain of events from the logs, and any relevant observations about the source code.\n\n"
    "EXPECTED FORMAT:\n"
    "{{\n"
    '  "user_message": "...",\n'
    '  "developer_diagnosis": "..."\n'
    "}}\n"
)

def trim_log_buffer(log_buffer: List[dict], max_chars: int = 100_000) -> str:
    """Serialize and trim a log buffer to fit within *max_chars* chronologically."""
    if not log_buffer:
        return ""

    selected: List[str] = []
    chars_used = 0

    for entry in reversed(log_buffer):
        entry_str = json.dumps(entry, ensure_ascii=False, default=str)
        entry_cost = len(entry_str) + (1 if selected else 0)
        if chars_used + entry_cost > max_chars:
            if chars_used == 0:
                trunc_marker = f"...[ENTRY TRUNCATED: original {len(entry_str)} chars]"
                avail = max_chars - len(trunc_marker)
                if avail > 0:
                    selected.append(entry_str[:avail] + trunc_marker)
            break

        selected.append(entry_str)
        chars_used += entry_cost

    selected.reverse()
    return "\n".join(selected)
