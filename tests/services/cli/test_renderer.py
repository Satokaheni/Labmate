from __future__ import annotations
import pytest
from services.cli.renderer import Renderer, extract_answer


def test_extract_answer_final():
    state = {"final_answer": "The answer is 42."}
    assert extract_answer(state) == "The answer is 42."


def test_extract_answer_root_result():
    state = {"goal_tree": {"root": {"result": "done"}}}
    assert extract_answer(state) == "done"


def test_extract_answer_fallback():
    state = {}
    result = extract_answer(state)
    assert isinstance(result, str)


def test_renderer_instantiates():
    r = Renderer()
    assert r is not None


class _FakeStream:
    def __init__(self, events):
        self._events = events

    async def events(self):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_stream_live_consumes_events_and_returns_renderer():
    from services.cli.renderer import Renderer
    r = Renderer()
    fake = _FakeStream([
        {"type": "turn.start", "task": "compute"},
        {"type": "answer.delta", "text": "Hello "},
        {"type": "answer.delta", "text": "world"},
        {"type": "turn.done", "status": "complete", "final_answer": ""},
    ])
    sr = await r.stream_live(fake)
    assert sr.answer_text == "Hello world"
    assert sr.status == "complete"
    assert sr.done is True


@pytest.mark.asyncio
async def test_stream_live_handles_empty_stream():
    from services.cli.renderer import Renderer
    r = Renderer()
    sr = await r.stream_live(_FakeStream([]))
    assert sr.answer_text == ""
    assert sr.done is False
