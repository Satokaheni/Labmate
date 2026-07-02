"""Resolve the model's actual per-request context window from llama-server.

Single source of truth for the context gauge: instead of hand-mirroring
serve-model.sh's `--ctx-size / --parallel` math into the `CTX_WINDOW` env, the
orchestrator asks llama-server what it actually loaded via `GET <base>/props`.

llama.cpp reports the PER-SLOT window (already `n_ctx / n_parallel`) under
`default_generation_settings.n_ctx`, so it is used verbatim. Older/edge builds
that only expose a top-level total `n_ctx` are handled by dividing it by
`total_slots`.

This is strictly best-effort: any failure (unreachable server, non-2xx, junk
payload, missing field) falls back to the supplied env value so startup is
never blocked.
"""

from __future__ import annotations

import logging

import httpx

_log = logging.getLogger("orchestrator.ctx_window")

# /props is a tiny local call; keep the startup probe snappy.
_DEFAULT_TIMEOUT_S = 3.0


def props_url(base_url: str) -> str:
    """Derive the llama-server /props URL from an OpenAI-compatible base.

    e.g. "http://localhost:8000/v1" -> "http://localhost:8000/props".
    """
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    root = root.rstrip("/")
    return f"{root}/props"


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        return None
    if isinstance(value, int | float) and value > 0:
        return int(value)
    return None


def parse_ctx_window(props: dict) -> int | None:
    """Extract the per-slot context window from a /props payload.

    Prefers `default_generation_settings.n_ctx` (already per-slot in llama.cpp);
    otherwise falls back to the top-level total `n_ctx` divided by `total_slots`
    (or the raw total if the slot count is absent). Returns None when no usable
    value is present.
    """
    if not isinstance(props, dict):
        return None

    dgs = props.get("default_generation_settings")
    if isinstance(dgs, dict):
        per_slot = _as_positive_int(dgs.get("n_ctx"))
        if per_slot is not None:
            return per_slot

    total = _as_positive_int(props.get("n_ctx"))
    if total is not None:
        slots = _as_positive_int(props.get("total_slots"))
        if slots is not None:
            return total // slots
        return total

    return None


async def resolve_ctx_window(
    base_url: str,
    *,
    fallback: int,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> int:
    """Query llama-server /props for the real per-slot window; fall back on failure.

    Never raises: on any error (connection, non-2xx, bad JSON, missing field) it
    logs at WARNING and returns `fallback`, so startup proceeds even when the
    inference server is unreachable.
    """
    url = props_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            measured = parse_ctx_window(resp.json())
        if measured is not None:
            _log.info("resolved context window from %s: %d tokens", url, measured)
            return measured
        _log.warning("no n_ctx in /props at %s — using fallback %d", url, fallback)
    except Exception as exc:  # best-effort — never block startup
        _log.warning("could not query %s (%s) — using fallback %d", url, exc, fallback)
    return fallback
