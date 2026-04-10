"""bot/core/modes.py

Unified Agent Persona Definitions.

Defines the six core modes with execution types, system prompts, and allowed tools.
This is the single source of truth for agent capabilities.
"""

from dataclasses import dataclass
from typing import List, Dict


@dataclass
class AgentMode:
    name: str
    execution_type: str
    system_prompt: str
    allowed_tools: List[str]


MODES: Dict[str, AgentMode] = {
    "Scout": AgentMode(
        name="Scout",
        execution_type="PROGRAMMATIC",
        system_prompt="You are the Scout. Find, extract, and structure raw web data.",
        allowed_tools=["system:complete_step", "system:initialize_checklist"]
    ),
    "Analyst": AgentMode(
        name="Analyst",
        execution_type="AUTONOMOUS",
        system_prompt="You are the Analyst. Run deep, multi-step chain-of-thought processing and synthesis.",
        allowed_tools=["system:switch_mode", "system:complete_step", "system:initialize_checklist", "library:vector_search", "browser:operator"]
    ),
    "Quant": AgentMode(
        name="Quant",
        execution_type="AUTONOMOUS",
        system_prompt="You are the Quant. Handle numerical reconciliation, SQL generation, and SEC EDGAR parsing.",
        allowed_tools=["system:switch_mode", "system:complete_step", "system:initialize_checklist"]
    ),
    "Editor": AgentMode(
        name="Editor",
        execution_type="AUTONOMOUS",
        system_prompt="You are the Editor. Modify, reorder, and format structured outputs using batch tools.",
        allowed_tools=["system:switch_mode", "system:complete_step", "system:initialize_checklist", "system:draft_editor", "library:vector_search"]
    ),
    "Herald": AgentMode(
        name="Herald",
        execution_type="AUTONOMOUS",
        system_prompt="You are the Herald. Format and broadcast finalized intelligence to external channels.",
        allowed_tools=["system:switch_mode", "system:complete_step", "system:initialize_checklist", "publisher"]
    ),
    "Archivist": AgentMode(
        name="Archivist",
        execution_type="AUTONOMOUS",
        system_prompt="You are the Archivist. Manage vector embeddings, RAG retrieval, and long-term memory curation.",
        allowed_tools=["system:switch_mode", "system:complete_step", "system:initialize_checklist", "library:vector_search"]
    ),
}
