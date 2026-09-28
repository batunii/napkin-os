import ast
import inspect
from pathlib import Path

import pytest

from napkin import registry
from napkin.capabilities import Capabilities, ModelCap, ResearchCap
from napkin.util import TaskError


def test_resolves_declared_handlers():
    mod, h = registry.resolve("extract_ask", {"pipeline": {"tasks": {"extract_ask": {"handler": "extract_ask@1"}}}})
    assert h == "extract_ask@1.0"
    assert registry.resolve("research_lens", {})[1] == "research_lens@1.0"  # the built-in map


@pytest.mark.parametrize("declared,etype", [("extract_ask@99", "unknown_handler"), ("nope@1", "unknown_handler"),
                                            ("research_lens@1", "unknown_handler"), ("extract_ask", "unknown_handler")])
def test_hard_errors_never_fall_through(declared, etype):
    with pytest.raises(TaskError) as e:
        registry.resolve("extract_ask", {"pipeline": {"tasks": {"extract_ask": {"handler": declared}}}})
    assert e.value.etype == etype


def test_undeclared_task_is_unknown_task():
    with pytest.raises(TaskError) as e:
        registry.resolve("synthesise_findings", {"pipeline": {"tasks": {"extract_ask": {"handler": "extract_ask@1"}}}})
    assert e.value.etype == "unknown_task"


def test_startup_fails_on_an_unregistered_handler_naming_the_majors(tmp_path):
    p = tmp_path / "pipeline.yaml"
    p.write_text("tasks:\n  extract_ask:\n    handler: extract_ask@7\n")
    with pytest.raises(registry.RegistryError, match=r"registered: \[1\]"):
        registry.resolve_at_startup([str(p)])
    p.write_text("tasks:\n  summon:\n    handler: extract_ask@1\n")
    with pytest.raises(registry.RegistryError, match="not in napkin.middleware/1"):
        registry.resolve_at_startup([str(p)])


def test_capability_version_is_checked(monkeypatch):
    from napkin.handlers import extract_ask
    monkeypatch.setattr(extract_ask, "CAPABILITY_MAJOR", 2)
    with pytest.raises(registry.RegistryError, match="capability"):
        registry.check_modules()


def test_no_capability_method_accepts_a_scope():
    for cls in (Capabilities, ModelCap, ResearchCap):
        for name, fn in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_") and name != "__init__":
                continue
            params = set(inspect.signature(fn).parameters)
            if cls is Capabilities and name == "__init__":
                continue  # the middleware binds the scope at construction
            assert not params & {"scope", "org", "brand", "tenant", "org_id", "tenant_id"}, (cls, name)
    from napkin.layers import Layers
    for name, fn in inspect.getmembers(Layers, inspect.isfunction):
        assert not set(inspect.signature(fn).parameters) & {"scope", "org", "tenant"}, name


FORBIDDEN = {"os", "socket", "subprocess", "sqlite3", "httpx", "requests", "urllib", "pathlib", "shutil", "anthropic"}


@pytest.mark.parametrize("path", sorted((Path(__file__).parents[1] / "napkin").glob("handlers/*.py")) +
                         sorted((Path(__file__).parents[1] / "napkin").glob("pipeline/*.py")))
def test_handlers_reach_nothing_outside_the_capability_object(path):
    """Handler and stage modules import no network, filesystem or environment
    access and never call open(); they get all of that through `caps`."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & FORBIDDEN, (path, node.lineno)
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] not in FORBIDDEN, (path, node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("open", "exec", "eval", "__import__"), (path, node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            raise AssertionError((path, node.lineno))


def test_review_tasks_resolve_for_a_document_whose_pipeline_predates_them():
    from napkin import registry
    clan = {"pipeline": {"tasks": {"extract_ask": {"handler": "extract_ask@1"}}}}
    for task in ("verify_finding", "correct_fact"):
        mod, handler = registry.resolve(task, clan)
        assert mod.TASK == task
    import pytest
    from napkin.util import TaskError
    with pytest.raises(TaskError):
        registry.resolve("research_lens", clan)
