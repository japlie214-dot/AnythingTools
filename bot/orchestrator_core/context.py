"""bot/orchestrator_core/context.py
SoM-aware context builder for orchestrator."""
from __future__ import annotations
from typing import Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from tools.registry import REGISTRY

@dataclass
class SoMContext:
    job_id: str
    tool_name: str
    tool_args: dict[str, Any]
    som_marker_range: tuple[int, int] | None = None
    element_hints: list[str] = field(default_factory=list)
    state: str = "INITIALIZING"

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "som_marker_range": self.som_marker_range,
            "element_hints": self.element_hints,
            "state": self.state,
        }

class SoMContextBuilder:
    def __init__(self, job_id: str):
        self._job_id = job_id
        self._context: SoMContext | None = None

    def initialize(self, tool_name: str, tool_args: dict[str, Any]) -> SoMContext:
        self._context = SoMContext(job_id=self._job_id, tool_name=tool_name, tool_args=tool_args, state="INITIALIZED")
        return self._context

    def inject_som_markers(self, marker_range: tuple[int, int]) -> None:
        if self._context: self._context.som_marker_range = marker_range

    def add_element_hint(self, hint: str) -> None:
        if self._context: self._context.element_hints.append(hint)

    def build_llm_prompt(self) -> str:
        if not self._context: return ""
        som_instructions = []
        if self._context.som_marker_range:
            start, end = self._context.som_marker_range
            som_instructions.append(f"## SoM Marker Range: {start} to {end}\nUse data-ai-id attributes from {start} to {end} for precise element targeting.")
        
        prompt_parts = [
            f"# Tool Execution Context",
            f"## Job ID: {self._job_id}",
            f"## Tool: {self._context.tool_name}",
            "## Tool Arguments:",
            json.dumps(self._context.tool_args, indent=2),
        ]
        if som_instructions:
            prompt_parts.extend(["## SoM Context:", *som_instructions])
            
        return "\n".join(prompt_parts)

    def get_context(self) -> SoMContext | None:
        return self._context