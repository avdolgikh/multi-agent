from __future__ import annotations

import ast
import builtins
import importlib
import re
import runpy
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


_FIXTURES_DIR = Path("fixtures") / "validation"
_SAMPLE_MODULE = _FIXTURES_DIR / "sample_module.py"
_RESEARCH_TOPICS = _FIXTURES_DIR / "research_topics.txt"
_REAL_IMPORT_MODULE = importlib.import_module
_REAL_BUILTIN_IMPORT = builtins.__import__
_STDLIB_MODULES = sys.stdlib_module_names | {"__future__"}


def _get_cli_module():
    module_name = "scripts.validate_vertical"
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise AssertionError(f"{module_name} is required by the vertical validation spec") from exc


def _get_function_source(source: str, node: ast.FunctionDef) -> str:
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return segment


def _block_entry_import(monkeypatch, *blocked_names: str) -> list[str]:
    attempts: list[str] = []

    def _matches_blocked(name: str) -> bool:
        for blocked in blocked_names:
            if name == blocked or name.startswith(f"{blocked}."):
                attempts.append(name)
                return True
        return False

    def _blocking_import_module(name: str, *args, **kwargs):
        _matches_blocked(name)
        return _REAL_IMPORT_MODULE(name, *args, **kwargs)

    def _blocking_builtin_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        _matches_blocked(name)
        return _REAL_BUILTIN_IMPORT(name, globals, locals, fromlist, level)

    monkeypatch.setattr(importlib, "import_module", _blocking_import_module)
    monkeypatch.setattr(builtins, "__import__", _blocking_builtin_import)
    return attempts


def _patch_module_main(monkeypatch, module_name: str, *, side_effect=None):
    mock_module = ModuleType(module_name)
    mock_main = MagicMock(side_effect=side_effect)
    mock_module.main = mock_main
    monkeypatch.setitem(sys.modules, module_name, mock_module)
    return mock_main


def _normalize_argument_value(arg):
    if isinstance(arg, (list, tuple)):
        return arg[0]
    return arg


def _find_eval_calls(node: ast.AST) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "eval"
    ]


def _assert_only_stdlib_imports(tree: ast.Module) -> None:
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_base = node.module or ""
            modules = [module_base.split(".")[0]]
        else:
            continue
        for module in modules:
            if not module:
                continue
            assert module in _STDLIB_MODULES, (
                f"non-stdlib import {module} found in {_SAMPLE_MODULE}"
            )


def _is_constant_node(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_constant_node(elt) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            _is_constant_node(key) and _is_constant_node(value)
            for key, value in zip(node.keys, node.values)
        )
    return False


def _assert_module_has_no_side_effects(tree: ast.Module) -> None:
    expr_seen = False
    for node in tree.body:
        if isinstance(node, ast.Expr):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), (
                "only docstrings should exist at module level"
            )
            assert not expr_seen, "multiple module docstrings detected"
            expr_seen = True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Assign):
            assert _is_constant_node(node.value), (
                "module-level assignments must only define constants"
            )
            continue
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            assert _is_constant_node(node.value), (
                "module-level annotations must only assign constants"
            )
            continue
        raise AssertionError("sample_module should not execute arbitrary statements on import")


def _function_contains_off_by_one_bug(node: ast.FunctionDef) -> bool:
    def _is_constant_one(candidate: ast.AST) -> bool:
        return (
            isinstance(candidate, ast.Constant)
            and isinstance(candidate.value, (int, float))
            and candidate.value == 1
        )

    def _binop_mentions_one(binop: ast.BinOp) -> bool:
        if not isinstance(binop.op, (ast.Sub, ast.Add)):
            return False
        return _is_constant_one(binop.left) or _is_constant_one(binop.right)

    def _slice_contains_offset_one(slice_node: ast.AST) -> bool:
        if isinstance(slice_node, ast.BinOp):
            return _binop_mentions_one(slice_node)
        if isinstance(slice_node, ast.Slice):
            for part in (slice_node.lower, slice_node.upper, slice_node.step):
                if part is not None and isinstance(part, ast.BinOp) and _binop_mentions_one(part):
                    return True
        return False

    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "range"
        ):
            for arg in inner.args:
                if isinstance(arg, ast.BinOp) and _binop_mentions_one(arg):
                    return True
        if isinstance(inner, ast.Subscript):
            if _slice_contains_offset_one(inner.slice):
                return True
        if isinstance(inner, ast.BinOp) and _binop_mentions_one(inner):
            return True
    return False


def _assert_elapsed_and_status_in_output(output: str) -> None:
    lowered = output.lower()
    assert "elapsed" in lowered
    assert "seconds" in lowered, "elapsed seconds not reported"
    assert re.search(r"\b\d+(\.\d+)?\b", lowered), "elapsed seconds do not include a numeric value"
    assert "exit status" in lowered


def test_sample_module_contains_the_expected_functions():
    assert _SAMPLE_MODULE.exists(), f"{_SAMPLE_MODULE} is missing"
    source = _SAMPLE_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SAMPLE_MODULE))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    for required in ("evaluate_expression", "compute_average_off_by_one", "clean_sum"):
        assert required in functions
        assert ast.get_docstring(functions[required]), f"{required} must have a docstring"

    _assert_only_stdlib_imports(tree)
    _assert_module_has_no_side_effects(tree)

    evaluate_src = _get_function_source(source, functions["evaluate_expression"])
    assert "eval(" in evaluate_src
    assert _find_eval_calls(functions["evaluate_expression"])
    for forbidden_user in ("compute_average_off_by_one", "clean_sum"):
        assert not _find_eval_calls(functions[forbidden_user]), (
            "only evaluate_expression may call eval"
        )

    assert _function_contains_off_by_one_bug(functions["compute_average_off_by_one"])

    clean_src = _get_function_source(source, functions["clean_sum"])
    for forbidden in ("eval", "exec", "os.system"):
        assert forbidden not in clean_src


def test_sample_module_import_has_no_runtime_output(capsys):
    assert _SAMPLE_MODULE.exists(), f"{_SAMPLE_MODULE} is missing"
    module_name = "fixtures_validation_sample_module"
    capsys.readouterr()
    runpy.run_path(str(_SAMPLE_MODULE), run_name=module_name)
    captured = capsys.readouterr()
    assert not captured.out.strip()
    assert not captured.err.strip()
    sys.modules.pop(module_name, None)


def test_research_topics_file_is_well_formed():
    assert _RESEARCH_TOPICS.exists(), f"{_RESEARCH_TOPICS} is missing"
    lines = _RESEARCH_TOPICS.read_text(encoding="utf-8").splitlines()
    trimmed = [line.strip() for line in lines]
    assert all(trimmed), "research topics file contains blank lines"
    assert len(trimmed) == len(lines), "research topics file has blank or comment lines"
    assert 3 <= len(trimmed) <= 5
    for topic in trimmed:
        assert 10 <= len(topic) <= 200
    assert not any(line.startswith("#") for line in trimmed), (
        "research topics file may not contain comments"
    )


def test_cli_help_mentions_subcommands(capsys):
    validate_vertical = _get_cli_module()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    output = captured.out.lower()
    assert "orchestration" in output
    assert "choreography" in output


def test_orchestration_requires_input_argument(capsys):
    validate_vertical = _get_cli_module()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["orchestration"])
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert "input" in (captured.err or captured.out).lower()


def test_orchestration_dry_run_skips_entry(monkeypatch, tmp_path, capsys):
    attempts = _block_entry_import(monkeypatch, "orchestration.code_analysis")
    mock_main = _patch_module_main(monkeypatch, "orchestration.code_analysis")
    validate_vertical = _get_cli_module()
    target = tmp_path / "demo.py"
    target.write_text("print('dry run example')")
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["orchestration", "--input", str(target), "--dry-run"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    summary = captured.out.lower()
    assert "dry run" in summary
    assert "orchestration" in summary
    assert str(target) in captured.out
    assert not mock_main.called
    assert not attempts


def test_orchestration_nonexistent_input_path_exits_nonzero(tmp_path):
    validate_vertical = _get_cli_module()
    missing = tmp_path / "missing_demo.py"
    assert not missing.exists()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["orchestration", "--input", str(missing)])
    assert excinfo.value.code != 0


def test_choreography_rejects_empty_topic(capsys):
    validate_vertical = _get_cli_module()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["choreography", "--topic", ""])
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert "topic" in (captured.err or captured.out).lower()


def test_choreography_dry_run_rejects_empty_topic(capsys):
    validate_vertical = _get_cli_module()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["choreography", "--topic", "", "--dry-run"])
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    output = (captured.err or captured.out).lower()
    assert "topic" in output


def test_choreography_dry_run_skips_entry(monkeypatch, capsys):
    attempts = _block_entry_import(monkeypatch, "choreography.research")
    mock_main = _patch_module_main(monkeypatch, "choreography.research")
    validate_vertical = _get_cli_module()
    topic = "event sourcing vs cqrs tradeoffs"
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["choreography", "--topic", topic, "--dry-run"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    summary = captured.out.lower()
    assert "dry run" in summary
    assert "choreography" in summary
    assert topic in captured.out
    assert not mock_main.called
    assert not attempts


def test_orchestration_dispatches_to_entry_point(monkeypatch, tmp_path):
    validate_vertical = _get_cli_module()
    mock_main = _patch_module_main(monkeypatch, "orchestration.code_analysis")
    target = tmp_path / "demo_orchestration.py"
    target.write_text("print('orchestration demo')")
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["orchestration", "--input", str(target)])
    assert excinfo.value.code == 0
    assert mock_main.called
    called_value = _normalize_argument_value(mock_main.call_args[0][0])
    assert Path(called_value).samefile(target)


def test_choreography_dispatches_to_entry_point(monkeypatch):
    validate_vertical = _get_cli_module()
    mock_main = _patch_module_main(monkeypatch, "choreography.research")
    topic = "event sourcing vs cqrs tradeoffs"
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["choreography", "--topic", topic])
    assert excinfo.value.code == 0
    assert mock_main.called
    called_value = _normalize_argument_value(mock_main.call_args[0][0])
    assert topic in str(called_value)


def test_orchestration_prints_elapsed_and_exit_status(monkeypatch, tmp_path, capsys):
    validate_vertical = _get_cli_module()
    mock_main = _patch_module_main(monkeypatch, "orchestration.code_analysis")
    target = tmp_path / "demo_orchestration.py"
    target.write_text("print('orchestration demo')")
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["orchestration", "--input", str(target)])
    assert excinfo.value.code == 0
    assert mock_main.called
    captured = capsys.readouterr()
    _assert_elapsed_and_status_in_output(f"{captured.out}{captured.err}")


def test_choreography_prints_elapsed_and_exit_status(monkeypatch, capsys):
    validate_vertical = _get_cli_module()
    mock_main = _patch_module_main(monkeypatch, "choreography.research")
    topic = "event sourcing vs cqrs tradeoffs"
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["choreography", "--topic", topic])
    assert excinfo.value.code == 0
    assert mock_main.called
    captured = capsys.readouterr()
    _assert_elapsed_and_status_in_output(f"{captured.out}{captured.err}")


def test_entry_point_exception_is_reported(monkeypatch, tmp_path, capsys):
    validate_vertical = _get_cli_module()
    mock_main = _patch_module_main(
        monkeypatch, "orchestration.code_analysis", side_effect=ValueError("boom")
    )
    target = tmp_path / "demo_error.py"
    target.write_text("print('error example')")
    with pytest.raises(SystemExit) as excinfo:
        validate_vertical.main(["orchestration", "--input", str(target)])
    assert excinfo.value.code != 0
    assert mock_main.called
    captured = capsys.readouterr()
    combined = f"{captured.out}{captured.err}"
    assert "ValueError" in combined
    assert "boom" in combined
