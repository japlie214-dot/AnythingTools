# utils/debugger_agent.py
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import config
from clients.llm import get_llm_client, LLMRequest
from clients.llm.utils import is_context_length_error
from utils.logger import get_dual_logger
from utils.logger.routing import DEBUGGER_FILE_MAP  # direct submodule import

import re

_REDACT_KEYS = {
    "password", "passwd", "token", "access_token", "api_key", "secret",
    "private_key", "privateKey", "authorization", "auth", "key",
}

_RE_KEY_PATTERN = re.compile(
    r'("(?P<k>{})"\s*:\s*)"([^\"]+)"'.format("|".join(re.escape(k) for k in _REDACT_KEYS)),
    flags=re.IGNORECASE,
)

def _redact_payload(payload: dict) -> str:
    try:
        s = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        s = repr(payload)
    if not s:
        return ""
    def repl(m: re.Match) -> str:
        return f"{m.group(1)}\"<REDACTED>\""
    try:
        return _RE_KEY_PATTERN.sub(repl, s)
    except Exception:
        return s

def trim_log_buffer(log_history: list[dict], max_chars: int) -> str:
    if not log_history:
        return ""
    lines = []
    for entry in log_history:
        ts = entry.get("timestamp") or entry.get("time") or ""
        lvl = entry.get("level") or entry.get("levelname") or ""
        tag = entry.get("tag") or entry.get("logger") or entry.get("name") or ""
        msg = entry.get("message") or entry.get("msg") or ""
        payload = entry.get("payload") or entry.get("payload_json") or entry.get("extra") or {}
        payload_s = _redact_payload(payload) if payload else ""
        lines.append(f"{ts} {lvl} {tag}: {msg} | payload: {payload_s}" if payload_s else f"{ts} {lvl} {tag}: {msg}")
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full
    tail = full[-max_chars:]
    nl = tail.find("\n")
    return tail[nl+1:] if nl >= 0 else tail

# All tags emitted here carry the "Debugger:" prefix — the Infinite Loop Guard
# in dual_log() fires an unconditional early-return for any such tag.
_log = get_dual_logger(__name__)

DEBUGGER_SYSTEM_PROMPT = (
    "You are a highly technical, code-focused diagnostic engineering agent.\n"
    "Your task is to analyze a system warning or error and produce a structured diagnosis.\n\n"
    "INSTRUCTIONS:\n"
    "1. Identify the failing function exactly as filename.py::function_name.\n"
    "2. Trace the exact causal chain through the provided log history entries in "
    "chronological order, citing specific entries by timestamp and tag.\n"
    "3. Cross-reference the provided source code to confirm the failure path at "
    "the line level.\n"
    "4. Produce ONLY a structured Markdown report with exactly these five sections:\n"
    "   ## Root Cause\n"
    "   ## Fault Location  (filename.py::function_name)\n"
    "   ## Causal Chain from Logs  (cite entries by timestamp + tag)\n"
    "   ## Source Code Observations  (specific line-level notes; no fixes or patches)\n"
    "   ## Recommended Inspection Points\n"
    "5. Do NOT produce corrective code, patches, refactored snippets, or any "
    "executable content. Diagnosis only."
)


async def run_debugger_agent(trigger_tag: str, log_history: list[dict]) -> None:
    """Async entry point.  Called exclusively via the three-tier dispatch in dual_log()."""
    try:
        # ── 1. Tag Resolution ────────────────────────────────────────────────
        # Default to the sentinel; override on first prefix match (insertion order).
        mapped_files: list[str] = DEBUGGER_FILE_MAP.get("DEFAULT", [])
        for prefix, files in DEBUGGER_FILE_MAP.items():
            if prefix != "DEFAULT" and trigger_tag.startswith(prefix):
                mapped_files = files
                break

        # ── 2. Establish budget and retry state ─────────────────────────
        budget: int = getattr(config, "DEBUGGER_AGENT_CONTEXT_CHAR_LIMIT", 400_000)
        min_budget: int = getattr(config, "MIN_EFFECTIVE_BUDGET", 4000)
        retried = False

        while True:
            # Use existing trim_log_buffer() for log-region trimming per GOLDEN RULE 2.
            # Conservative: allocate half the budget to logs on first pass (deterministic).
            log_history_str: str = trim_log_buffer(log_history, max_chars=budget // 2)
            chars_used: int = len(log_history_str)

            # File-reading guillotine (existing algorithm preserved; only chars_used input differs)
            accumulated_source: list[str] = []
            for filepath in mapped_files:
                if not os.path.exists(filepath):
                    _log.dual_log(
                        tag="Debugger:ContextAssembly",
                        message=f"Mapped file not found, skipping: {filepath}",
                        level="WARNING",
                        payload={"file": str(filepath)},
                    )
                    continue
                try:
                    with open(filepath, "r", encoding="utf-8") as fh:
                        content = fh.read()
                except Exception as read_err:
                    _log.dual_log(
                        tag="Debugger:ContextAssembly",
                        message=f"Cannot read {filepath}: {read_err}",
                        level="WARNING",
                        payload={"file": str(filepath), "error": str(read_err)},
                    )
                    continue

                file_header = f"\n### File: {filepath}\n```python\n"
                trunc_marker = f"\n...[FILE TRUNCATED: original {len(content)} chars]\n```\n"
                file_footer = "\n```\n"
                full_block = f"{file_header}{content}{file_footer}"
                projected = chars_used + len(full_block)

                if projected > budget:
                    if not accumulated_source:
                        avail_for_content = max(
                            0,
                            budget - chars_used - len(file_header) - len(trunc_marker),
                        )
                        truncated_block = f"{file_header}{content[:avail_for_content]}{trunc_marker}"
                        accumulated_source.append(truncated_block)
                    break

                accumulated_source.append(full_block)
                chars_used = projected

            source_code_str: str = "".join(accumulated_source)
            user_prompt: str = (
                f"### LOG HISTORY ({len(log_history_str)} chars)\n"
                f"```json\n{log_history_str}\n```\n\n"
                f"### SOURCE CODE ({len(source_code_str)} chars)\n"
                f"{source_code_str}"
            )

            llm = get_llm_client("azure")
            request = LLMRequest(
                messages=[
                    {"role": "system", "content": DEBUGGER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                model=getattr(config, "AZURE_DEPLOYMENT", "gpt-5.4-mini"),
            )

            try:
                response = await llm.complete_chat(request)
                break  # success — exit retry loop
            except Exception as exc:
                # Only one retry allowed; detect context-length errors and retry once with halved budget.
                if not retried and is_context_length_error(exc):
                    retried = True
                    new_budget = max(int(budget * 0.5), min_budget)
                    _log.dual_log(
                        tag="Debugger:ContextHalving",
                        message="Context-limit error intercepted; halving budget.",
                        level="WARNING",
                        payload={"old_budget": budget, "new_budget": new_budget},
                    )
                    budget = new_budget
                    continue

                # On any other failure or if retry already used → abort final report generation.
                _log.dual_log(
                    tag="Debugger:Error",
                    message="Debugger Agent aborted after context-retry failure",
                    level="ERROR",
                    exc_info=exc,
                    payload={"error": str(exc)},
                )
                return

        # ── 5. Write Markdown report ─────────────────────────────────────────
        report_dir = Path("logs/debug_reports")
        report_dir.mkdir(parents=True, exist_ok=True)

        sanitized_tag = trigger_tag.replace(":", "_")
        timestamp     = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path   = report_dir / f"debugger_{sanitized_tag}_{timestamp}.md"

        report_path.write_text(response.content, encoding="utf-8")

        # INFO level → the Phase 3 level gate exits before any trigger logic fires.
        _log.dual_log(
            tag="Debugger:Report",
            message=f"Debugger Agent report generated: {report_path}",
            level="INFO",
            payload={"report_path": str(report_path)},
        )

    except Exception as agent_err:
        # Tag prefix "Debugger:" guarantees the Infinite Loop Guard fires on
        # re-entry; this branch can never cause recursive agent invocation.
        _log.dual_log(
            tag="Debugger:Error",
            message=f"Debugger Agent failed: {agent_err}",
            level="ERROR",
            exc_info=agent_err,
            payload={"error": str(agent_err)},
        )
