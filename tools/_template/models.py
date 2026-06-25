# tools/_template/models.py
"""Minimal Pydantic input model for the tool template.

Copy this file alongside tool.py when scaffolding a new tool. Replace
TemplateInput with <YourTool>Input and add fields per your tool's
instructions contract.
"""
from typing import Literal
from pydantic import BaseModel, Field


class TemplateInput(BaseModel):
    """Input model for the template tool.

    The command field discriminates the sub-path inside run(). The
    instructions field carries the command-specific payload.
    """
    command: Literal["demo"] = "demo"
    instructions: dict = Field(default_factory=dict)
