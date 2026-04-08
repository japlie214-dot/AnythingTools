# tools/search/__init__.py
"""Package initializer for the search tool.
Re-exports SearchTool for compatibility with existing imports like
`from tools.search import SearchTool`.
"""
from .tool import SearchTool

__all__ = ["SearchTool"]
