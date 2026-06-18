import logging
import re
import sys

from figma_client import DesignToken, TokenSet

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("design-token-transform.transformer")


class TokenTransformer:
    def transform(self, tokens: TokenSet, format: str) -> str:
        if format == "tailwind":
            return self._to_tailwind(tokens)
        if format == "css-vars":
            return self._to_css_vars(tokens)
        if format == "shadcn":
            return self._to_shadcn(tokens)
        raise ValueError(
            f"unknown format {format!r}; expected 'tailwind', 'css-vars', or 'shadcn'"
        )

    @staticmethod
    def _slug(name: str) -> str:
        # "Primary / Blue 500" -> "primary-blue-500"
        s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower())
        return s.strip("-") or "token"

    @staticmethod
    def _by_category(tokens: TokenSet, category: str) -> list[DesignToken]:
        return [t for t in tokens.tokens if t.category == category]

    def _to_css_vars(self, tokens: TokenSet) -> str:
        lines = [":root {"]
        prefix = {
            "color": "color",
            "typography": "font-size",
            "spacing": "spacing",
            "radius": "radius",
            "shadow": "shadow",
        }
        for category in ["color", "typography", "spacing", "radius", "shadow"]:
            items = self._by_category(tokens, category)
            if not items:
                continue
            lines.append(f"  /* {category} */")
            for tok in items:
                var = f"--{prefix[category]}-{self._slug(tok.name)}"
                lines.append(f"  {var}: {tok.value};")
        lines.append("}")
        return "\n".join(lines)

    def _to_tailwind(self, tokens: TokenSet) -> str:
        groups = {
            "colors": self._by_category(tokens, "color"),
            "fontSize": self._by_category(tokens, "typography"),
            "spacing": self._by_category(tokens, "spacing"),
            "borderRadius": self._by_category(tokens, "radius"),
        }
        sections: list[str] = []
        for key, items in groups.items():
            if not items:
                continue
            entries = ",\n".join(
                f'        "{self._slug(t.name)}": "{t.value}"' for t in items
            )
            sections.append(f"      {key}: {{\n{entries}\n      }}")
        body = ",\n".join(sections)
        return (
            "/** @type {import('tailwindcss').Config} */\n"
            "module.exports = {\n"
            "  theme: {\n"
            "    extend: {\n"
            f"{body}\n"
            "    }\n"
            "  }\n"
            "}"
        )

    def _to_shadcn(self, tokens: TokenSet) -> str:
        lines = ["@layer base {", "  :root {"]
        for tok in self._by_category(tokens, "color"):
            h, s, l = self._hex_to_hsl(tok.value)
            lines.append(f"    --{self._slug(tok.name)}: {h} {s}% {l}%;")
        radii = self._by_category(tokens, "radius")
        if radii:
            # shadcn uses a single --radius; take the first deterministic one.
            lines.append(f"    --radius: {radii[0].value};")
        lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def _hex_to_hsl(hex_value: str) -> tuple[int, int, int]:
        h = hex_value.lstrip("#")
        r = int(h[0:2], 16) / 255
        g = int(h[2:4], 16) / 255
        b = int(h[4:6], 16) / 255
        mx, mn = max(r, g, b), min(r, g, b)
        l = (mx + mn) / 2
        if mx == mn:
            hue = sat = 0.0
        else:
            d = mx - mn
            sat = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
            if mx == r:
                hue = (g - b) / d + (6 if g < b else 0)
            elif mx == g:
                hue = (b - r) / d + 2
            else:
                hue = (r - g) / d + 4
            hue /= 6
        return round(hue * 360), round(sat * 100), round(l * 100)
