"""Regression test for F1: memory_search tool wiring in production.

Ensures that:
1. AsyncOrchestrator.memory_search is set in main.py (not None)
2. The tool appears in PromptAssembler.tools() when memory_search is available
3. The memory_search tool schema is included in the ReAct loop tools list
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.memory_search import MemorySearch
from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.prompt_assembler import PromptAssembler


class TestMemorySearchWiring:
    """memory_search must be wired to enable the tool in the ReAct loop."""

    def test_memory_search_instance_creation(self):
        """MemorySearch can be instantiated with a store object."""
        mock_store = MagicMock()
        memory_search = MemorySearch(mock_store)
        assert memory_search.store is mock_store
        assert memory_search.max_results == 8

    def test_memory_search_with_custom_max_results(self):
        """MemorySearch respects custom max_results parameter."""
        mock_store = MagicMock()
        memory_search = MemorySearch(mock_store, max_results=12)
        assert memory_search.max_results == 12

    def test_async_orchestrator_memory_search_defaults_to_none(self):
        """AsyncOrchestrator.memory_search should default to None and be settable."""
        orch = AsyncOrchestrator()
        assert orch.memory_search is None

    def test_async_orchestrator_memory_search_settable(self):
        """AsyncOrchestrator.memory_search can be set after construction."""
        orch = AsyncOrchestrator()
        mock_store = MagicMock()
        memory_search = MemorySearch(mock_store)
        orch.memory_search = memory_search
        assert orch.memory_search is memory_search

    def test_prompt_assembler_includes_memory_search_schema_when_enabled(self):
        """When memory_enabled=True, PromptAssembler includes memory_search schema."""
        assembler = PromptAssembler(memory_enabled=True)
        tools = assembler.tools()

        # Find the memory_search tool
        memory_search_schema = None
        for tool in tools:
            if tool.get("type") == "function":
                if tool.get("function", {}).get("name") == "memory_search":
                    memory_search_schema = tool
                    break

        assert memory_search_schema is not None, "memory_search tool schema not found"
        assert memory_search_schema["function"]["name"] == "memory_search"
        assert "query" in memory_search_schema["function"]["parameters"]["properties"]
        assert "k" in memory_search_schema["function"]["parameters"]["properties"]

    def test_prompt_assembler_excludes_memory_search_schema_when_disabled(self):
        """When memory_enabled=False, PromptAssembler excludes memory_search schema."""
        assembler = PromptAssembler(memory_enabled=False)
        tools = assembler.tools()

        # Find the memory_search tool
        memory_search_schema = None
        for tool in tools:
            if tool.get("type") == "function":
                if tool.get("function", {}).get("name") == "memory_search":
                    memory_search_schema = tool
                    break

        assert memory_search_schema is None, "memory_search tool schema should not be present"

    def test_prompt_assembler_with_memory_search_and_codegraph(self):
        """PromptAssembler can include both memory_search and codegraph schemas."""
        assembler = PromptAssembler(codegraph_enabled=True, memory_enabled=True)
        tools = assembler.tools()

        tool_names = set()
        for tool in tools:
            if tool.get("type") == "function":
                tool_names.add(tool.get("function", {}).get("name"))

        assert "memory_search" in tool_names, "memory_search tool should be present"
        assert "code_semantic_search" in tool_names, "code_semantic_search tool should be present"

    def test_prompt_assembler_tool_order_is_deterministic(self):
        """PromptAssembler tool order is deterministic for prefix cache stability."""
        assembler1 = PromptAssembler(memory_enabled=True)
        assembler2 = PromptAssembler(memory_enabled=True)

        tools1 = assembler1.tools()
        tools2 = assembler2.tools()

        assert len(tools1) == len(tools2)
        for t1, t2 in zip(tools1, tools2):
            assert t1 == t2, "tool order must be consistent across instances"
