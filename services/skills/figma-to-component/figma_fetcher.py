"""FigmaFetcher: fetch a Figma node via the REST API and parse it into a typed tree.

CRITICAL: stdout is the JSON-RPC channel. NEVER print(); log to sys.stderr only.
"""
from __future__ import annotations

import logging
import os

import httpx

from models import ComponentSpec, FigmaNode

log = logging.getLogger("figma-to-component.fetcher")

FIGMA_API_BASE = os.getenv("FIGMA_API_BASE", "https://api.figma.com")
DEFAULT_TIMEOUT_S = 30.0


class FigmaTokenMissingError(RuntimeError):
    """Raised when FIGMA_ACCESS_TOKEN is not configured."""


def _require_token() -> str:
    token = os.getenv("FIGMA_ACCESS_TOKEN")
    if not token:
        raise FigmaTokenMissingError(
            "FIGMA_ACCESS_TOKEN is not set. Create a Figma personal access token "
            "(Figma -> Settings -> Personal access tokens) and export it as "
            "FIGMA_ACCESS_TOKEN before using the figma-to-component skill."
        )
    return token


class FigmaFetcher:
    def __init__(self, token: str | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._token = token or _require_token()
        self._timeout = timeout

    async def get_node(self, file_key: str, node_id: str) -> ComponentSpec:
        url = f"{FIGMA_API_BASE}/v1/files/{file_key}/nodes"
        headers = {"X-Figma-Token": self._token}
        params = {"ids": node_id}

        log.info("fetching figma node file=%s node=%s", file_key, node_id)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            payload = resp.json()

        nodes = payload.get("nodes") or {}
        entry = nodes.get(node_id)
        if not entry or "document" not in entry:
            raise ValueError(
                f"node {node_id!r} not found in file {file_key!r} "
                f"(returned keys: {list(nodes)})"
            )

        document = entry["document"]
        node = self._parse_node(document)
        tokens = self._collect_tokens(node)
        return ComponentSpec(node=node, file_key=file_key, node_id=node_id, tokens=tokens)

    def _parse_node(self, raw: dict) -> FigmaNode:
        return FigmaNode(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            type=raw.get("type", "UNKNOWN"),
            layout=self._parse_layout(raw),
            fills=self._parse_fills(raw),
            text_style=self._parse_text_style(raw),
            variables=self._parse_variables(raw),
            children=[self._parse_node(c) for c in raw.get("children", [])],
        )

    @staticmethod
    def _parse_layout(raw: dict) -> dict:
        mode = raw.get("layoutMode")
        if not mode:
            return {}
        return {
            "direction": mode,
            "gap": raw.get("itemSpacing", 0),
            "padding": {
                "top": raw.get("paddingTop", 0),
                "right": raw.get("paddingRight", 0),
                "bottom": raw.get("paddingBottom", 0),
                "left": raw.get("paddingLeft", 0),
            },
            "align_items": raw.get("counterAxisAlignItems"),
            "justify_content": raw.get("primaryAxisAlignItems"),
        }

    @staticmethod
    def _parse_fills(raw: dict) -> list[dict]:
        fills = raw.get("fills")
        return fills if isinstance(fills, list) else []

    @staticmethod
    def _parse_text_style(raw: dict) -> dict | None:
        if raw.get("type") != "TEXT":
            return None
        style = raw.get("style") or {}
        return {
            "characters": raw.get("characters", ""),
            "font_family": style.get("fontFamily"),
            "font_size": style.get("fontSize"),
            "font_weight": style.get("fontWeight"),
            "line_height_px": style.get("lineHeightPx"),
            "text_align": style.get("textAlignHorizontal"),
        }

    @staticmethod
    def _parse_variables(raw: dict) -> dict:
        bound = raw.get("boundVariables")
        return bound if isinstance(bound, dict) else {}

    def _collect_tokens(self, node: FigmaNode) -> dict:
        tokens: dict = {}

        def walk(n: FigmaNode) -> None:
            for field, alias in (n.variables or {}).items():
                if isinstance(alias, dict) and alias.get("id"):
                    tokens[alias["id"]] = {"field": field, **alias}
            for child in n.children:
                walk(child)

        walk(node)
        return tokens
