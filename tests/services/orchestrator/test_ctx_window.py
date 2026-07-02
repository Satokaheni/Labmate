"""Startup context-window resolution.

The orchestrator queries llama-server's /props endpoint for the ACTUAL loaded
per-slot context window instead of trusting the CTX_WINDOW env, so the frontend
context bar tracks the served model automatically (no manual env sync on a
model/context swap). These cover the URL builder, the pure props parser, and the
best-effort HTTP resolver (which must fall back to the env value, never break
startup, when llama-server is unreachable or returns junk).
"""

import httpx
import respx

from services.orchestrator import ctx_window

# --- props_url: derive the /props URL from an OpenAI-compatible base ---


def test_props_url_strips_v1_suffix():
    assert ctx_window.props_url("http://localhost:8000/v1") == "http://localhost:8000/props"


def test_props_url_strips_trailing_slashes():
    assert ctx_window.props_url("http://localhost:8000/v1/") == "http://localhost:8000/props"
    assert ctx_window.props_url("http://localhost:8000/") == "http://localhost:8000/props"


def test_props_url_without_v1():
    assert ctx_window.props_url("http://host:8000") == "http://host:8000/props"


# --- parse_ctx_window: pure extraction of the per-slot window ---


def test_parse_prefers_default_generation_settings_n_ctx():
    # llama.cpp reports the PER-SLOT window here already (n_ctx / n_parallel), so it
    # is used verbatim — no division needed.
    props = {"default_generation_settings": {"n_ctx": 262144}, "n_ctx": 262144, "total_slots": 1}
    assert ctx_window.parse_ctx_window(props) == 262144


def test_parse_falls_back_to_top_level_n_ctx_over_slots():
    # No per-slot value → derive it from the total context divided by the slot count.
    props = {"n_ctx": 262144, "total_slots": 2}
    assert ctx_window.parse_ctx_window(props) == 131072


def test_parse_top_level_n_ctx_without_slots():
    props = {"n_ctx": 65536}
    assert ctx_window.parse_ctx_window(props) == 65536


def test_parse_returns_none_when_no_ctx_present():
    assert ctx_window.parse_ctx_window({"model_path": "/x.gguf"}) is None
    assert ctx_window.parse_ctx_window({}) is None


def test_parse_ignores_nonpositive_values():
    assert ctx_window.parse_ctx_window({"default_generation_settings": {"n_ctx": 0}}) is None
    assert ctx_window.parse_ctx_window({"n_ctx": -1}) is None


# --- resolve_ctx_window: best-effort HTTP with env fallback ---


@respx.mock
async def test_resolve_returns_measured_window():
    respx.get("http://localhost:8000/props").mock(
        return_value=httpx.Response(200, json={"default_generation_settings": {"n_ctx": 262144}})
    )
    got = await ctx_window.resolve_ctx_window("http://localhost:8000/v1", fallback=131072)
    assert got == 262144


@respx.mock
async def test_resolve_falls_back_when_unreachable():
    respx.get("http://localhost:8000/props").mock(side_effect=httpx.ConnectError("refused"))
    got = await ctx_window.resolve_ctx_window("http://localhost:8000/v1", fallback=131072)
    assert got == 131072


@respx.mock
async def test_resolve_falls_back_on_5xx():
    respx.get("http://localhost:8000/props").mock(return_value=httpx.Response(503))
    got = await ctx_window.resolve_ctx_window("http://localhost:8000/v1", fallback=99999)
    assert got == 99999


@respx.mock
async def test_resolve_falls_back_when_props_has_no_ctx():
    respx.get("http://localhost:8000/props").mock(
        return_value=httpx.Response(200, json={"model_path": "/x.gguf"})
    )
    got = await ctx_window.resolve_ctx_window("http://localhost:8000/v1", fallback=131072)
    assert got == 131072
