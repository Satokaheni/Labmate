import json
import pytest

pytestmark = pytest.mark.mocked


@pytest.mark.asyncio
async def test_call_tool_returns_json(monkeypatch):
    import server as srv
    from executor import ExecutionResult

    class FakeExec:
        def run_python(self, code, timeout=30, packages=[]):
            return ExecutionResult(
                stdout="hi", stderr="", exit_code=0, duration_ms=5
            )

    monkeypatch.setattr(srv, "get_executor", lambda: FakeExec())
    out = await srv.call_tool("code_sandbox.run_python", {"code": "print('hi')"})
    payload = json.loads(out[0].text)
    assert payload["stdout"] == "hi"
    assert payload["exit_code"] == 0


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_returns_error(monkeypatch):
    import server as srv
    from executor import ExecutionResult

    class FakeExec:
        pass

    monkeypatch.setattr(srv, "get_executor", lambda: FakeExec())
    out = await srv.call_tool("code_sandbox.nonexistent", {"code": "x"})
    payload = json.loads(out[0].text)
    assert "error" in payload
