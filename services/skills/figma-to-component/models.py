"""Pydantic data models for the figma-to-component skill.

CRITICAL: never print() or write to stdout.
"""
from __future__ import annotations
from pydantic import BaseModel, Field


class FigmaNode(BaseModel):
    id: str
    name: str
    type: str
    layout: dict = Field(default_factory=dict)
    fills: list[dict] = Field(default_factory=list)
    text_style: dict | None = None
    children: list["FigmaNode"] = Field(default_factory=list)
    variables: dict = Field(default_factory=dict)


FigmaNode.model_rebuild()


class ComponentSpec(BaseModel):
    node: FigmaNode
    file_key: str
    node_id: str
    tokens: dict = Field(default_factory=dict)


class ComponentResult(BaseModel):
    component_code: str
    component_name: str
    props_interface: str
    framework: str
