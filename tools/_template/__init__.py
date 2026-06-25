# tools/_template/__init__.py
# Intentionally empty. Makes tools/_template/ a real package so it can be
# imported via `from tools._template.tool import TemplateTool` during
# onboarding demos. This package is NOT registered in tools/registry.py
# (verified at tools/registry.py — the registry uses an explicit whitelist
# that does not include "_template").
