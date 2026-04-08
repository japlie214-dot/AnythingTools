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

        for module_info in pkgutil.iter_modules([str(package_dir)]):
            name = module_info.name
            if name in {"base", "registry", "__init__"}:
                continue

            module_path = package_dir / name
            try:
                if (module_path / "__init__.py").exists():
                    # Prefer the canonical tool submodule when present.
                    try:
                        module = importlib.import_module(f"tools.{name}.tool")
                    except Exception:
                        module = importlib.import_module(f"tools.{name}")
                else:
                    module = importlib.import_module(f"tools.{name}")
            except Exception as e:
                log.dual_log(tag="Registry:Load", message=f"Failed to import tools.{name}: {e}", level="WARNING", payload={"module": name})
                continue

            # Attempt to capture INPUT_MODEL.schema() if provided by the module.
            input_schema: Optional[Dict[str, Any]] = None
            try:
                InputModel = getattr(module, "INPUT_MODEL", None)
                if InputModel is not None and hasattr(InputModel, "schema"):
                    try:
                        input_schema = InputModel.schema()
                    except Exception as e:
                        log.dual_log(tag="Registry:Schema", message=f"Failed to serialize INPUT_MODEL for {name}: {e}", level="WARNING", payload={"module": name})
            except Exception:
                input_schema = None

            # Attempt to read a human description from tools.<name>.Skill.desc
            description: Optional[str] = None
            try:
                skill_mod = importlib.import_module(f"tools.{name}.Skill")
                description = getattr(skill_mod, "desc", None)
            except Exception:
                # Fallback: read SKILL.md file if present (legacy)
                skill_md = package_dir / name / "SKILL.md"
                if skill_md.exists():
                    try:
                        description = skill_md.read_text(encoding='utf-8')
                    except Exception:
                        description = None

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


# Singleton registry instance
REGISTRY = ToolRegistry()
