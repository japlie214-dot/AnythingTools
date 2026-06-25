# utils/observability/__init__.py
"""Activity-Driven Observability — Developer Contract.

This module is the single source of truth for the observability framework.
The contract is layered (TL;DR → Recipe → Formal Rules) so it serves three
audiences: new devs scanning the TL;DR, integrators following the Recipe,
and reviewers/auditors cross-referencing the §-numbered rules.

======================================================================
TL;DR
======================================================================

1. Every entry point's business logic is expressed as a sequence of named
   Activities. The Accumulator is created at the invocation boundary and
   threaded through every Activity. When observability is inactive, the
   Accumulator is a no-op with zero overhead.

2. Canonical import:
       from utils.observability import (
           activity, ActivityAccumulator, ActivityRecord, LineageReport,
           bind_accumulator, unbind_accumulator, get_current_accumulator,
           serialize_safe, DEFAULT_MAX_CHARS, truncate_and_mask,
       )

3. Three rules that always hold:
   - The @activity decorator NEVER swallows exceptions — it records FAILED
     then re-raises.
   - The accumulator NEVER raises into tool code — all internal errors are
     caught and logged.
   - The error field is NEVER truncated — it is the diagnostic lifeline.

======================================================================
Integration Recipe
======================================================================

To add observability to a new tool:

1. Copy `tools/_template/tool.py` to `tools/<your_tool>/tool.py`.
2. Define an `INPUT_MODEL` (Pydantic) and a `run()` async method.
3. Decompose `run()` into named sub-methods, each decorated with
   `@activity("Verb Phrase")`. Activity names are verb-phrases
   ("Validate Input", "Fetch Rows", "Build Payload").
4. Activities MUST raise on failure (never swallow). The decorator records
   FAILED then re-raises; your entry point's except block decides the
   terminal status.
5. Return JSON-serializable values from every activity. For payloads
   larger than 50 000 chars per top-level key, write an artifact via
   `write_artifact(...)` and return the path — the Lineage is a trace,
   not a data store.

The Accumulator is created for you by `bot/engine/worker.py::_run_job`
when `capture_lineage=true` is set on the job. You do not create or bind
the Accumulator yourself.

======================================================================
Formal Rules (§-numbered; cited from across the codebase)
======================================================================

§4.3.a — ActivityRecord shape
  - activity_name: str (verb-phrase)
  - status: Literal["PASSED", "FAILED"]
  - inputs: dict | None (named-parameter-bound via inspect.signature.bind;
    truncated + masked per §4.3.d)
  - outputs: Any | None (return value; truncated + masked per §4.3.d;
    None on FAILED)
  - error: str | None (failure message; NEVER truncated — diagnostic lifeline)
  - started_at, ended_at: ISO 8601 UTC strings
  - duration_ms: float
  Implemented in `accumulator.py::ActivityRecord`.

§4.3.b — @activity decorator behavior
  - Reads the accumulator from `contextvars.ContextVar` (not from kwargs).
  - Sync and async functions supported via `inspect.iscoroutinefunction`.
  - If no accumulator is active: zero-overhead pass-through.
  - If accumulator is active:
      - Extracts named inputs via `inspect.signature.bind` (excluding
        `self` and `accumulator`).
      - Calls the wrapped function.
      - On success: records PASSED with outputs.
      - On exception: records FAILED with `str(e)` as error, then RE-RAISES.
  Implemented in `activity_decorator.py::activity`.

§4.3.c — ContextVar propagation
  - The accumulator is bound via `bind_accumulator(acc)` which returns a
    `contextvars.Token`. The Token MUST be passed to `unbind_accumulator(token)`
    in a `finally` block.
  - Propagation crosses `asyncio.Runner`, `to_thread_with_context`, and
    `spawn_thread_with_context` (all call `contextvars.copy_context()`).
  - It does NOT cross the API-handler → polling-thread boundary (plain
    `threading.Thread`). The `capture_lineage` boolean crosses that boundary
    via the `jobs.args_json` column.
  - Ref: https://docs.python.org/3/library/contextvars.html
  Implemented in `context.py`.

§4.3.d — Truncation and masking
  - Per-key character cap: 50 000 chars (configurable via
    `LINEAGE_MAX_STRING_CHARS`).
  - Truncation is applied PER INDIVIDUAL TOP-LEVEL KEY VALUE (for dicts) or
    PER TOP-LEVEL VALUE (for lists and other types) — NOT to the total
    payload. A 500 000-char payload where each individual top-level key's
    value is under 50 000 chars is recorded in full.
  - Auto-masking (runs before truncation, replaces value with a placeholder
    that preserves the key name and a size annotation):
      * Base64 strings ≥ 1 000 chars from `[A-Za-z0-9+/=\s]`.
      * Float-vector arrays ≥ 10 elements (catches embeddings of 64+).
      * Binary-adjacent content: hex blobs, JWTs, raw byte sequences
        (non-printable chars in first 100 chars).
      * Six secret patterns: OpenAI keys, GitHub tokens, AWS keys, JWTs,
        credit cards.
      * 27 sensitive key names (api_key, token, password, snowflake_private_key,
        etc.) — masked at the key level regardless of value.
  - The `error` field is NEVER truncated.
  Implemented in `masking.py::truncate_and_mask` (structural recursion) and
  `masking.py::_cap_top_level_value` (top-level cap for dict and list).
  Orchestrated by `masking.py::serialize_safe`.

§4.3.e — LineageReport shape
  - summary: dict with `status` ("PASSED" | "FAILED"), `total_activities_executed`,
    `passed`, `failed`, `dropped_count`, `job_id`, `tool_name`, `started_at`,
    `ended_at`.
  - lineage: list[ActivityRecord] in execution order.
  - business_response_snapshot: Any | None (the tool's parsed return value;
    null if any Activity has status FAILED).
  Implemented in `accumulator.py::LineageReport`.

§4.4 — Observation vs. validation
  - The Lineage is a pure observation artifact. It records what data entered
    and exited each Activity. It does not assert, validate, or judge.
  - Tests live in `tests/` (not in `utils/observability/`). External HTTP
    runners that assert lineage shape are an anti-pattern and have been
    removed.

§4.5 — Maintenance
  - If you change `serialize_safe`, `truncate_and_mask`,
    `_cap_top_level_value`, `activity`, `ActivityAccumulator.record`, or
    `LineageReport`, update the corresponding §4.3.x rule in this file in
    the same PR.

======================================================================
Public API re-exports
======================================================================

Pure pass-through. No new logic — only makes the canonical import work.
"""

from utils.observability.accumulator import (
    ActivityAccumulator,
    ActivityRecord,
    LineageReport,
)
from utils.observability.activity_decorator import activity
from utils.observability.context import (
    get_current_accumulator,
    bind_accumulator,
    unbind_accumulator,
)
from utils.observability.masking import (
    serialize_safe,
    truncate_and_mask,
    DEFAULT_MAX_CHARS,
)

__all__ = [
    "activity",
    "ActivityAccumulator",
    "ActivityRecord",
    "LineageReport",
    "bind_accumulator",
    "unbind_accumulator",
    "get_current_accumulator",
    "serialize_safe",
    "truncate_and_mask",
    "DEFAULT_MAX_CHARS",
]
