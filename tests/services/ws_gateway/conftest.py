import pytest
from argon2 import PasswordHasher

from services.orchestrator.inproc_bus import EventBus, ResultRegistry, SignalRegistry
from services.ws_gateway.user_store import InMemoryUserStore


class StubRuntime:
    """A minimal stand-in for `OrchestratorProcess` (the real `runtime`).

    Wires real in-process `EventBus`/`SignalRegistry`/`ResultRegistry` primitives
    (the same ones the gateway uses against the real orchestrator in 7c) but
    replaces goal *processing* with a recording `submit_goal` — tests drive the
    relay side by publishing canned events directly onto `bus` for the returned
    task_id, exactly as the real orchestrator would via `events.py`.
    """

    def __init__(self) -> None:
        self.bus = EventBus()
        self.signals = SignalRegistry()
        self.results = ResultRegistry()
        self.submitted: list[dict] = []

    async def submit_goal(self, payload: dict) -> str:
        task_id = payload.get("task_id") or f"task-{len(self.submitted)}"
        payload = {**payload, "task_id": task_id}
        self.submitted.append(payload)
        return task_id


@pytest.fixture
def runtime():
    return StubRuntime()


@pytest.fixture
async def seeded_store():
    store = InMemoryUserStore()
    ph = PasswordHasher()
    await store.create(
        email="admin@labmate.local",
        display_name="Admin",
        password_hash=ph.hash("correct-horse"),
        role="admin",
    )
    return store
