# tools/_template/tool.py
"""Tool template — copy-paste starting point for new tools.

Activity-Driven Observability:
  Every business-logic step is a @activity-decorated method. The decorator
  records inputs/outputs to the Accumulator (when one is bound) and is a
  zero-overhead pass-through otherwise. Activities raise on failure — the
  decorator records FAILED then re-raises; the entry point's except block
  decides the terminal status.

See: utils/observability/__init__.py (the Developer Contract).
"""
import json
from typing import Any

from tools.base import BaseTool, ToolExecutionError, ToolValidationError
from utils.logger import get_dual_logger
from utils.observability import activity
from .models import TemplateInput

log = get_dual_logger(__name__)


class TemplateTool(BaseTool):
    """Copy this class to scaffold a new tool.

    Steps to convert this into a real tool:
    1. Rename `TemplateTool` → `<YourTool>Tool`. Update `name` and `INPUT_MODEL`.
    2. Add real fields to `models.py::TemplateInput` (rename it too).
    3. Replace the activity methods below with your tool's real decomposition.
    4. Register the tool in `tools/registry.py` (add to the whitelist).
    """
    name = "template"
    INPUT_MODEL = TemplateInput

    def is_resumable(self, args: dict[str, Any]) -> bool:
        return False

    @activity("Validate Template Input")
    def _validate_input(self, args: dict, job_id: str):
        """Parse and validate args against INPUT_MODEL. Raises ToolValidationError."""
        try:
            validated = TemplateInput.model_validate(args)
        except Exception as e:
            raise ToolValidationError(
                f"Invalid input: {e}",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Check the command and instructions shape.",
            ) from e
        return validated

    @activity("Perform Demo Work")
    def _perform_demo_work(self, instructions: dict, job_id: str) -> dict:
        """Replace this with your tool's real business logic.

        Raises ToolExecutionError on failure. The @activity decorator records
        FAILED with str(e) as the error, then re-raises — the entry point's
        except block decides the terminal status.
        """
        # Example: validate a required field, raise on missing.
        required_key = instructions.get("required_key")
        if not required_key:
            raise ToolExecutionError(
                "Missing required_key in instructions.",
                tool_name=self.name,
                job_id=job_id,
                next_steps="Provide 'required_key' in the instructions payload.",
            )
        return {"echo": required_key}

    @activity("Build Demo Payload")
    def _build_demo_payload(self, work_result: dict) -> dict:
        """Compose the final JSON payload returned to the caller.

        Per the Developer Contract §4.3.d: payloads larger than 50 000 chars
        per top-level key MUST be written as artifacts via write_artifact
        and the path returned in the payload. The Lineage is a trace, not
        a data store.
        """
        return {"result": work_result}

    async def run(self, args: dict[str, Any], telemetry: Any, **kwargs) -> str:
        """Entry point. The worker calls this method; the Accumulator is
        already bound by bot/engine/worker.py::_run_job when capture_lineage
        is true. Do NOT create or bind an Accumulator here.
        """
        job_id = kwargs.get("job_id", "")

        # Step 1: Validate (raises ToolValidationError on bad input).
        validated = self._validate_input(args, job_id)

        # Step 2: Perform work (raises ToolExecutionError on failure).
        work_result = self._perform_demo_work(validated.instructions, job_id)

        # Step 3: Build payload (pure transformation, no I/O).
        payload = self._build_demo_payload(work_result)

        return json.dumps(payload, ensure_ascii=False, default=str)
