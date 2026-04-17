# deprecated/tools/polymarket/__init__.py
"""Package initializer for the polymarket tool.
Re-exports PolymarketTool for compatibility with existing imports.
"""
from .tool import PolymarketTool

__all__ = ["PolymarketTool"]
