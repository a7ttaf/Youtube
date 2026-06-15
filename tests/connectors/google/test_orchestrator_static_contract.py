"""Static contract tests for the B2 connector orchestrator public surface."""

import ast
from pathlib import Path

ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "backend"
    / "ums_smart_revenue"
    / "connectors"
    / "runs"
    / "orchestrator.py"
)
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.IfExp,
    ast.BoolOp,
    ast.Match,
)


def _top_level_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _has_call_to(function: ast.FunctionDef, name: str) -> bool:
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        called = node.func
        if isinstance(called, ast.Name) and called.id == name:
            return True
        if isinstance(called, ast.Attribute) and called.attr == name:
            return True
    return False


def test_run_one_stays_thin_without_deepsource_complexity_suppression() -> None:
    source = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    run_one = _top_level_function(tree, "run_one")
    run_one_source = ast.get_source_segment(source, run_one) or ""

    assert "skipcq: PY-R1000" not in run_one_source
    assert sum(isinstance(node, BRANCH_NODES) for node in ast.walk(run_one)) == 0
    assert _has_call_to(run_one, "_run_one_with_credentials")
