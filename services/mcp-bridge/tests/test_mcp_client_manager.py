import asyncio
import pytest


@pytest.mark.asyncio
async def test_call_tool_enqueues_req_with_distinct_future():
    """Each call_tool() enqueues a _Req with a distinct Future."""
    from mcp_client_manager import MCPClientManager, _Req
    from mcp import StdioServerParameters

    params = StdioServerParameters(command='cat', args=[])
    mgr = MCPClientManager(params)
    loop = asyncio.get_running_loop()

    fut1 = loop.create_future()
    fut2 = loop.create_future()
    await mgr._inbox.put(_Req('tool_a', {}, fut1))
    await mgr._inbox.put(_Req('tool_b', {}, fut2))

    assert mgr._inbox.qsize() == 2
    req_a = mgr._inbox.get_nowait()
    req_b = mgr._inbox.get_nowait()
    assert req_a.name == 'tool_a'
    assert req_b.name == 'tool_b'
    assert req_a.future is fut1
    assert req_b.future is fut2


@pytest.mark.asyncio
async def test_drain_with_sets_exception_on_all_pending():
    """_drain_with sets exception on all queued futures."""
    from mcp_client_manager import MCPClientManager, CircuitOpenError, _Req
    from mcp import StdioServerParameters

    params = StdioServerParameters(command='cat', args=[])
    mgr = MCPClientManager(params)
    loop = asyncio.get_running_loop()

    futures = [loop.create_future() for _ in range(3)]
    for i, f in enumerate(futures):
        await mgr._inbox.put(_Req(f'tool_{i}', {}, f))

    err = CircuitOpenError('test circuit open')
    mgr._drain_with(err)

    for f in futures:
        assert f.done()
        assert isinstance(f.exception(), CircuitOpenError)


@pytest.mark.asyncio
async def test_shutdown_does_not_raise():
    """shutdown() must complete cleanly without RuntimeError."""
    from mcp_client_manager import MCPClientManager
    from mcp import StdioServerParameters

    # 'sleep 10' keeps the process alive so the manager can start
    params = StdioServerParameters(command='sleep', args=['10'])
    mgr = MCPClientManager(params)
    await mgr.start()
    await asyncio.sleep(0.2)
    # Must not raise RuntimeError about cancel scopes
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_breaker_open_after_max_failures():
    """Circuit breaker opens after max_failures crashes."""
    from mcp_client_manager import MCPClientManager
    from mcp import StdioServerParameters

    # 'false' exits immediately with code 1 — simulates crash
    params = StdioServerParameters(command='false', args=[])
    mgr = MCPClientManager(params, max_failures=2, window=60.0)
    await mgr.start()
    # Give it time to crash twice and open the circuit
    await asyncio.sleep(4.0)
    assert mgr._breaker_open(), "circuit should be open after repeated crashes"
    await mgr.shutdown()
