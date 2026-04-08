# tools package initializer
"""Tools package.

This package provides the tools namespace under which individual
tool packages live. It intentionally does NOT expose prompt templates
or constants because prompts belong inside each tool package or in
`utils/` for shared prompts. Keeping prompts out of `tools.__init__`
prevents accidental import of prompt-only modules when scanning the
tools package at import-time.
"""

__all__ = [
    "registry",  # dynamic discovery lives in tools.registry
]
