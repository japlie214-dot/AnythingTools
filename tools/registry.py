# tools/registry.py
"""Registry that loads only the whitelisted core tools.

Tools are expected to reside in the ``tools/`` package (excluding ``base`` and
``registry`` modules) and subclass ``BaseTool``. The registry stores tool
classes (not shared instances) and exposes helpers to instantiate a fresh tool
per job. It attempts to extract an optional ``INPUT_MODEL`` from the tool
module and a ``desc`` string from a sibling ``Skill.py`` module to use as the
manifest description.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path
from typing import Any, Dict, Optional, Type

from tools.base import BaseTool
from utils.logger.core import get_dual_logger

log = get_dual_logger(__name__)


class ToolRegistry:
    """Registry of available tools.

    Internal structure:
      _tools: Dict[str, Dict] = {
          "tool_name": {"cls": <class>, "input_schema": {...}|None, "module": "tools.foo", "description": "..."}
      }
    """

    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._discovery_results: Dict[str, Dict[str, Any]] = {}
        self._previous_discovery_results: Dict[str, Dict[str, Any]] = {}

    def diagnostic_list(self) -> Dict[str, Dict[str, Any]]:
        return self._discovery_results.copy()

    def load_all(self) -> None:
        """Import explicitly whitelisted core tools with state diffing.
        
        State diffing prevents log spam on repeated requests by only logging
        tool states that have changed since the last invocation.
        """
        self._previous_discovery_results = self._discovery_results.copy()
        
        # Use temporary dicts to prevent transient 404s/503s during concurrent API requests
        temp_tools = {}
        temp_discovery = {}
        
        package_dir = Path(__file__).parent

        # Load explicitly whitelisted core tools (No backup)
        core_tools = ["scraper", "draft_editor", "publisher", "batch_reader"]
        
        for tool_dir in core_tools:
            self._discover_tool(package_dir, tool_dir, temp_tools, temp_discovery)

        # Atomic swap avoids race conditions without locking overhead
        self._tools = temp_tools
        self._discovery_results = temp_discovery

        self._log_state_changes()

    def _discover_tool(self, package_dir: Path, tool_dir: str, temp_tools: dict, temp_discovery: dict) -> None:
        child = package_dir / tool_dir
        module_name = f"tools.{child.name}"
        
        if not child.exists():
            temp_discovery[tool_dir] = {
                "status": "MISSING",
                "module": module_name,
                "error": f"Directory not found: {child}"
            }
            return

        found_any = False
            
        try:
            module = importlib.import_module(module_name)
            if self._register_module_tools(module, temp_tools, temp_discovery, primary_tool=tool_dir, module_name=module_name):
                found_any = True
        except Exception as e:
            temp_discovery[tool_dir] = {
                "status": "FAILED",
                "module": module_name,
                "error": str(e),
                "exception_type": type(e).__name__
            }

        # Attempt to import conventional submodules
        for sub in ("tool", "Skill"):
            sub_module_name = f"tools.{child.name}.{sub}"
            try:
                submod = importlib.import_module(sub_module_name)
                if self._register_module_tools(submod, temp_tools, temp_discovery, primary_tool=tool_dir, module_name=sub_module_name):
                    found_any = True
            except ModuleNotFoundError as e:
                # If the specific submodule (e.g. "Skill.py") just doesn't exist, ignore it.
                if getattr(e, "name", None) == sub_module_name:
                    pass
                else:
                    temp_discovery[tool_dir] = {
                        "status": "FAILED",
                        "module": sub_module_name,
                        "error": str(e),
                        "exception_type": type(e).__name__
                    }
            except Exception as e:
                # Catch actual syntax/runtime errors and record them for 503 HTTP responses
                temp_discovery[tool_dir] = {
                    "status": "FAILED",
                    "module": sub_module_name,
                    "error": str(e),
                    "exception_type": type(e).__name__
                }
                
        if not found_any and tool_dir not in temp_discovery:
            temp_discovery[tool_dir] = {
                "status": "REJECTED",
                "module": module_name,
                "error": "No valid BaseTool subclasses found in module"
            }

    def _log_state_changes(self) -> None:
        for tool_name, current in self._discovery_results.items():
            prev = self._previous_discovery_results.get(tool_name)
            # Only log if state changed
            if prev != current:
                status = current.get("status")
                if status == "LOADED":
                    log.dual_log(tag="Registry:Register", message=f"Registered tool: {tool_name}", level="INFO", payload=current)
                elif status in ("FAILED", "REJECTED", "MISSING"):
                    log.dual_log(tag="Registry:Discover", message=f"Tool discovery failed for {tool_name}: {current.get('error')}", level="ERROR", payload={"tool": tool_name, "status": status})


    def _register_module_tools(self, module, temp_tools: dict, temp_discovery: dict, primary_tool: str = None, module_name: str = None) -> bool:
        if module_name is None:
            module_name = module.__name__

        # Attempt to capture INPUT_MODEL.schema() if provided by the module.
        input_schema: Optional[Dict[str, Any]] = None
        try:
            InputModel = getattr(module, "INPUT_MODEL", None)
            if InputModel is not None and hasattr(InputModel, "schema"):
                try:
                    input_schema = InputModel.schema()
                except Exception as e:
                    log.dual_log(tag="Registry:Schema", message=f"Failed to serialize INPUT_MODEL for {module_name}: {e}", level="WARNING", payload={"module": module_name})
        except Exception:
            input_schema = None

        # Attempt to read a human description from tools.<name>.Skill.desc (legacy)
        # For new actions, description can come from module.__doc__ or left empty.
        description: Optional[str] = None
        try:
            # Try to extract description from module docstring if no Skill module
            description = module.__doc__
        except Exception:
            pass

        found_any = False
        # Register concrete BaseTool subclasses defined in the module.
        for _, obj in inspect.getmembers(module, inspect.isclass):
            # Only interested in concrete subclasses defined in this module.
            if obj is BaseTool:
                continue
            try:
                if not issubclass(obj, BaseTool):
                    continue
            except Exception:
                continue
            if obj.__module__ != module.__name__:
                continue
            if inspect.isabstract(obj):
                continue

            # Derive the tool name. Prefer a class attribute 'name'. Fall back
            # to instantiating the class (best-effort).
            tool_name = getattr(obj, "name", None)
            if not tool_name:
                try:
                    inst = obj()  # many tools have parameterless constructors
                    tool_name = getattr(inst, "name", None)
                except Exception as e:
                    temp_discovery[primary_tool or "unknown"] = {
                        "status": "REJECTED",
                        "module": module_name,
                        "error": f"Skipping tool class {obj} - instantiation failed: {e}"
                    }
                    continue

            # Validate tool name against Azure OpenAI naming constraints
            if not tool_name or not re.match(r'^[a-zA-Z0-9_-]+$', tool_name):
                temp_discovery[tool_name or primary_tool or "unknown"] = {
                    "status": "REJECTED",
                    "module": module_name,
                    "error": f"Invalid tool name '{tool_name}'. Must match ^[a-zA-Z0-9_-]+$"
                }
                continue

            temp_tools[tool_name] = {
                "cls": obj,
                "input_schema": input_schema,
                "module": module.__name__,
                "description": description,
            }
            temp_discovery[tool_name] = {
                "status": "LOADED",
                "module": module_name,
                "error": None
            }
            found_any = True

            log.dual_log(tag="Registry:Register", message=f"Registered tool: {tool_name}", level="DEBUG", payload={"module": module.__name__, "class": obj.__name__})

        return found_any

    def get_tool_class(self, name: str) -> Optional[Type[BaseTool]]:
        meta = self._tools.get(name)
        return meta.get("cls") if meta else None


    def create_tool_instance(self, name: str, **kwargs) -> Optional[BaseTool]:
        """Instantiate a fresh tool for a job.

        Extra kwargs are passed to the tool class constructor. Returns None if the
        tool is not found or instantiation fails.
        """
        cls = self.get_tool_class(name)
        if cls is None:
            return None
        try:
            return cls(**kwargs)
        except Exception:
            try:
                return cls()
            except Exception as e:
                log.dual_log(tag="Registry:Instantiate", message=f"Failed to instantiate tool {name}: {e}", level="ERROR", payload={"tool": name})
                return None

    def schema_list(self) -> list[Dict[str, Any]]:
        """Return a minimal MCP-style manifest for all registered tools."""
        entries: list[Dict[str, Any]] = []
        for tool_name, meta in self._tools.items():
            input_schema = meta.get("input_schema")
            if not input_schema:
                input_schema = {"type": "object", "properties": {}, "required": []}
            description = meta.get("description")
            if not description:
                description = f"Dynamically discovered tool {tool_name}"
            
            som_instructions = ""
            if tool_name in {"scraper", "browser_task"}:
                som_instructions = " This tool uses SoM element targeting. Use data-ai-id attributes."

            entries.append({
                "name": tool_name,
                "description": description + som_instructions,
                "input_schema": input_schema,
            })
        return entries

    def get_som_tools(self) -> list[str]:
        """Return list of tools that support SoM integration."""
        return [name for name in self._tools.keys() if name in {"scraper", "browser_task"}]


    def get_responses_tools(self, tool_names: list[str]) -> list[Dict[str, Any]]:
        """Return tools in the exact format required by Azure OpenAI Responses API (and OpenAI Responses API)."""
        if not tool_names:
            return []

        # Build a quick lookup of schemas by name for O(1) access.
        schema_map = {s["name"]: s for s in self.schema_list()}

        tools: list[Dict[str, Any]] = []
        for name in tool_names:
            schema = schema_map.get(name)
            if not schema:
                log.dual_log(
                    tag="Registry:Tools",
                    message=f"Allowed tool '{name}' not found in registry",
                    level="WARNING",
                    payload={"tool": name, "status": "MISSING"},
                )
                continue
            tools.append({
                "type": "function",
                "name": name,
                "description": schema.get("description") or f"Dynamically discovered tool {name}",
                "parameters": schema.get("input_schema") or {"type": "object", "properties": {}, "required": []},
            })
        return tools

# Singleton registry instance
REGISTRY = ToolRegistry()
