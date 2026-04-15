# tools/registry.py
"""Dynamic registry that discovers and loads all tool implementations.

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
import pkgutil
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

    def load_all(self) -> None:
        """Import all modules under ``tools`` and register any ``BaseTool`` subclasses.

        This method is resilient: failures to import a particular tool module
        are logged and do not abort discovery of other modules.
        """
        self._tools.clear()
        package_dir = Path(__file__).parent

        # 1. Agent-action discovery under tools/actions/<scope>/
        actions_dir = package_dir / "actions"
        if actions_dir.exists() and actions_dir.is_dir():
            for scope in actions_dir.iterdir():
                if not scope.is_dir() or scope.name.startswith("_"):
                    continue
                # Discover modules inside each scope folder
                for module_info in pkgutil.iter_modules([str(scope)]):
                    module_name = module_info.name
                    module_path = scope / module_name
                    try:
                        # All action modules use tools.actions.<scope>.<module>
                        module = importlib.import_module(f"tools.actions.{scope.name}.{module_name}")
                    except Exception as e:
                        log.dual_log(tag="Registry:Load", message=f"Failed to import actions.{scope.name}.{module_name}: {e}", level="WARNING", payload={"module": f"{scope.name}.{module_name}"})
                        continue
                    self._register_module_tools(module)

                    # If the discovered module is a package, attempt to import its inner 'tool' submodule
                    if module_info.ispkg:
                        try:
                            submod = importlib.import_module(f"tools.actions.{scope.name}.{module_name}.tool")
                            self._register_module_tools(submod)
                        except Exception:
                            # Silently ignore missing 'tool' submodule – not all packages expose it
                            pass

        # 3. Public tools discovery: top-level tool modules and packages under tools/
        #    (e.g., tools/browser_task, tools/research). This ensures public entry
        #    points are exposed in the /api manifest as intended.
        for child in package_dir.iterdir():
            # Skip helper modules
            if child.name in ('__init__.py', 'registry.py', 'base.py'):
                continue

            # If it's a package (directory with __init__.py), import package and
            # also attempt to import common submodules (tool.py, Skill.py) inside it.
            if child.is_dir():
                module_name = f"tools.{child.name}"
                try:
                    module = importlib.import_module(module_name)
                    self._register_module_tools(module)
                except Exception as e:
                    log.dual_log(tag="Registry:Load", message=f"Failed to import public tool package {module_name}: {e}", level="DEBUG", payload={"module": module_name})

                # Attempt to import conventional submodules inside the package, e.g. tool.py
                for sub in ("tool", "Skill"):
                    try:
                        submod = importlib.import_module(f"tools.{child.name}.{sub}")
                        self._register_module_tools(submod)
                    except Exception:
                        pass
                continue

            # If it's a top-level .py module file, import it
            if child.is_file() and child.suffix == '.py':
                module_name = f"tools.{child.stem}"
                try:
                    module = importlib.import_module(module_name)
                    self._register_module_tools(module)
                except Exception as e:
                    log.dual_log(tag="Registry:Load", message=f"Failed to import public tool module {module_name}: {e}", level="DEBUG", payload={"module": module_name})
                continue

    def _register_module_tools(self, module):
        # Attempt to capture INPUT_MODEL.schema() if provided by the module.
        input_schema: Optional[Dict[str, Any]] = None
        try:
            InputModel = getattr(module, "INPUT_MODEL", None)
            if InputModel is not None and hasattr(InputModel, "schema"):
                try:
                    input_schema = InputModel.schema()
                except Exception as e:
                    log.dual_log(tag="Registry:Schema", message=f"Failed to serialize INPUT_MODEL for {module.__name__}: {e}", level="WARNING", payload={"module": module.__name__})
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
                    log.dual_log(tag="Registry:Register", message=f"Skipping tool class because name not found and instantiation failed: {obj}: {e}", level="WARNING", payload={"class": f"{obj}"})
                    continue

            # Validate tool name against Azure OpenAI naming constraints
            if not tool_name or not re.match(r'^[a-zA-Z0-9_-]+$', tool_name):
                raise ValueError(f"Invalid tool name '{tool_name}'. Tool names must match ^[a-zA-Z0-9_-]+$")

            self._tools[tool_name] = {
                "cls": obj,
                "input_schema": input_schema,
                "module": module.__name__,
                "description": description,
            }
            log.dual_log(tag="Registry:Register", message=f"Registered tool: {tool_name}", level="DEBUG", payload={"module": module.__name__, "class": obj.__name__})

    def get_tool_class(self, name: str) -> Optional[Type[BaseTool]]:
        meta = self._tools.get(name)
        return meta.get("cls") if meta else None

    def get_actions(self, scope: str) -> list[Dict[str, Any]]:
        """Return tools filtered by namespace (e.g. scope='browser' matches tools.actions.browser.*)."""
        entries = []
        for name, meta in self._tools.items():
            mod = meta.get("module", "")
            if mod.startswith(f"tools.actions.{scope}") or mod.startswith(f"tools.{scope}"):
                input_schema = meta.get("input_schema") or {"type": "object", "properties": {}, "required": []}
                entries.append({
                    "name": name,
                    "description": meta.get("description", ""),
                    "input_schema": input_schema
                })
        return entries

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
        """Return a minimal MCP-style manifest for all registered tools.

        For tools that expose an INPUT_MODEL, the serialized Pydantic schema is
        used as `input_schema`. Otherwise a permissive empty object schema is
        returned to indicate free-form input.
        """
        entries: list[Dict[str, Any]] = []
        for tool_name, meta in self._tools.items():
            input_schema = meta.get("input_schema")
            if not input_schema:
                input_schema = {"type": "object", "properties": {}, "required": []}
            description = meta.get("description") or f"Dynamically discovered tool {tool_name}"
            entries.append({
                "name": tool_name,
                "description": description,
                "input_schema": input_schema,
            })
        return entries


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
