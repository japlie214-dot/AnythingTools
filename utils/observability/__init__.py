# utils/observability/__init__.py
"""Activity-Driven Observability framework.

Provides:
- ActivityAccumulator: per-job context object that records named activities.
- @activity decorator: wraps functions to auto-record their execution.
- LineageReport: the verification artifact consumed by synthetic tracers.
"""
