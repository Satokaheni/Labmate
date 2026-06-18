import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

# CRITICAL: stderr only. Configure before anything that may log.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("design-token-transform.figma")


class DesignToken(BaseModel):
    name: str
    category: str  # 'color' | 'typography' | 'spacing' | 'radius' | 'shadow'
    value: str     # raw value e.g. "#FF5733" or "16px" or "400"
    description: str = ""


class TokenSet(BaseModel):
    source: str  # figma file key
    tokens: list[DesignToken]
    extracted_at: str


class FigmaClient:
    def __init__(self) -> None:
        self._token = os.getenv("FIGMA_ACCESS_TOKEN")
        if not self._token:
            raise RuntimeError(
                "FIGMA_ACCESS_TOKEN is not set. Export a Figma personal access "
                "token before using the design-token-transform skill."
            )
        self._base = os.getenv("FIGMA_API_BASE", "https://api.figma.com/v1")
        log.info("FigmaClient configured, base=%s", self._base)

    async def get_file_tokens(
        self, file_key: str, node_id: str | None = None
    ) -> TokenSet:
        headers = {"X-Figma-Token": self._token}
        async with httpx.AsyncClient(timeout=30.0) as client:
            if node_id:
                url = f"{self._base}/files/{file_key}/nodes"
                resp = await client.get(url, headers=headers, params={"ids": node_id})
            else:
                url = f"{self._base}/files/{file_key}"
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # Whole-file responses put the tree under "document"; node responses
        # put each requested node under "nodes"[id]["document"].
        if node_id:
            roots = [
                entry["document"]
                for entry in data.get("nodes", {}).values()
                if entry and "document" in entry
            ]
        else:
            roots = [data["document"]] if "document" in data else []

        tokens: list[DesignToken] = []
        for root in roots:
            tokens.extend(self._extract_tokens_from_node(root))

        # Deduplicate by (category, name), keep first; deterministic order.
        seen: set[tuple[str, str]] = set()
        unique: list[DesignToken] = []
        for tok in sorted(tokens, key=lambda t: (t.category, t.name)):
            key = (tok.category, tok.name)
            if key not in seen:
                seen.add(key)
                unique.append(tok)

        log.info("extracted %d unique tokens from %s", len(unique), file_key)
        return TokenSet(
            source=file_key,
            tokens=unique,
            extracted_at=datetime.now(timezone.utc).isoformat(),
        )

    def _extract_tokens_from_node(self, node: dict) -> list[DesignToken]:
        tokens: list[DesignToken] = []
        name = node.get("name", "").strip() or node.get("id", "unnamed")

        # --- Color: first solid fill ---
        for fill in node.get("fills", []) or []:
            if fill.get("type") == "SOLID" and fill.get("visible", True):
                color = fill.get("color", {})
                hex_value = self._rgba_to_hex(color, fill.get("opacity"))
                tokens.append(
                    DesignToken(
                        name=name,
                        category="color",
                        value=hex_value,
                        description=f"fill from node {node.get('id', '')}",
                    )
                )
                break

        # --- Typography: text style block ---
        style = node.get("style")
        if isinstance(style, dict) and "fontSize" in style:
            size = style["fontSize"]
            tokens.append(
                DesignToken(
                    name=name, category="typography",
                    value=f"{self._num(size)}px",
                    description=f"fontFamily={style.get('fontFamily', '')} "
                                f"weight={style.get('fontWeight', '')}",
                )
            )

        # --- Radius ---
        radius = node.get("cornerRadius")
        if isinstance(radius, (int, float)):
            tokens.append(
                DesignToken(name=name, category="radius",
                            value=f"{self._num(radius)}px")
            )

        # --- Spacing: auto-layout item spacing ---
        spacing = node.get("itemSpacing")
        if isinstance(spacing, (int, float)):
            tokens.append(
                DesignToken(name=name, category="spacing",
                            value=f"{self._num(spacing)}px")
            )

        # Recurse into children.
        for child in node.get("children", []) or []:
            tokens.extend(self._extract_tokens_from_node(child))

        return tokens

    @staticmethod
    def _rgba_to_hex(color: dict, opacity: float | None) -> str:
        r = round(color.get("r", 0) * 255)
        g = round(color.get("g", 0) * 255)
        b = round(color.get("b", 0) * 255)
        a = color.get("a", 1) if opacity is None else opacity
        if a is not None and a < 1:
            return f"#{r:02X}{g:02X}{b:02X}{round(a * 255):02X}"
        return f"#{r:02X}{g:02X}{b:02X}"

    @staticmethod
    def _num(value: float) -> str:
        # Drop trailing .0 so 16.0 -> "16" but 1.5 -> "1.5". Keeps output stable.
        return str(int(value)) if float(value).is_integer() else str(value)
