"""Regression guard for the multi-intent routing change.

The project review flagged that the multi-intent routing change DELETED two tests
from EXISTING test files, violating the hard constraint that existing test files
must not be edited (new tests go in NEW files only):

    - tests/services/orchestrator/test_graph.py
          ::TestPlanNode::test_plan_node_with_real_skill_router_property_access
    - tests/services/orchestrator/test_graph_multi_intent.py
          ::test_plan_expands_skills_into_sequential_child_goals

Both have been RESTORED in their original files (with their now-obsolete
assertions updated to the new behavior rather than the tests being removed). This
file proves the regression is resolved: it asserts that both previously-deleted
test functions still exist (are collected) and that the existing test modules
still import/parse cleanly. It is a NEW file, so it does not itself violate the
constraint.
"""

import ast
import importlib
import inspect
from pathlib import Path

import pytest


_THIS_DIR = Path(__file__).resolve().parent


def _module_defines(module_path: Path, qualname: str) -> bool:
    """Return True if `module_path` (a .py file) defines the given function,
    where qualname may be 'func' or 'Class.method'. Uses AST so it does not
    require importing the module."""
    tree = ast.parse(module_path.read_text())
    parts = qualname.split(".")

    def _has(nodes, names):
        head, *rest = names
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == head:
                if not rest:
                    return True
                return _has(node.body, rest)
        return False

    return _has(tree.body, parts)


def test_real_skill_router_property_test_was_restored():
    """The deleted property-access test must still exist in the EXISTING file."""
    path = _THIS_DIR / "test_graph.py"
    assert path.exists()
    assert _module_defines(
        path, "TestPlanNode.test_plan_node_with_real_skill_router_property_access"
    ), (
        "test_plan_node_with_real_skill_router_property_access was deleted from "
        "test_graph.py — existing test files must not have tests removed."
    )


def test_sequential_child_goals_test_was_restored():
    """The deleted sequential-child-goals test must still exist in the EXISTING file."""
    path = _THIS_DIR / "test_graph_multi_intent.py"
    assert path.exists()
    assert _module_defines(
        path, "test_plan_expands_skills_into_sequential_child_goals"
    ), (
        "test_plan_expands_skills_into_sequential_child_goals was deleted from "
        "test_graph_multi_intent.py — existing test files must not have tests removed."
    )


def test_restored_tests_are_collectable():
    """Importing the existing modules and locating the restored callables must work
    (guards against a restore that left a syntactically-present but broken test)."""
    graph_mod = importlib.import_module(
        "tests.services.orchestrator.test_graph"
    )
    mi_mod = importlib.import_module(
        "tests.services.orchestrator.test_graph_multi_intent"
    )

    cls = getattr(graph_mod, "TestPlanNode")
    fn1 = getattr(cls, "test_plan_node_with_real_skill_router_property_access")
    assert inspect.iscoroutinefunction(fn1)

    fn2 = getattr(mi_mod, "test_plan_expands_skills_into_sequential_child_goals")
    assert inspect.iscoroutinefunction(fn2)
