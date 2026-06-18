"""Shared pydantic models for the screenshot_to_component skill.

CRITICAL: this module is loaded inside an MCP stdio child process.
NEVER print() or write to stdout. All logging goes to sys.stderr.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class UIElement(BaseModel):
    # 'header' | 'nav' | 'sidebar' | 'card' | 'button' | 'input' | 'text' | 'image' | 'other'
    label: str
    bounds: BoundingBox
    children: list["UIElement"] = Field(default_factory=list)
    description: str = ""  # e.g. "primary CTA button with blue background"


class GroundingResult(BaseModel):
    elements: list[UIElement] = Field(default_factory=list)
    image_width: int
    image_height: int


class LayoutPlan(BaseModel):
    root_component: str = "flex flex-col min-h-screen"  # root Tailwind container classes
    sections: list[dict] = Field(default_factory=list)  # hierarchical section descriptions
    color_palette: list[str] = Field(default_factory=list)  # inferred hex colors
    typography_notes: str = ""


class GenerationResult(BaseModel):
    component_code: str
    framework: str
    output_path: str | None = None


# UIElement references itself in `children`; rebuild to resolve the forward ref.
UIElement.model_rebuild()
