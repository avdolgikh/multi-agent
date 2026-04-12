from __future__ import annotations

import ast
import asyncio
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from core.agents import AgentResult, AgentTask, BaseAgent

from .models import (
    AnalysisReport,
    ClassDescriptor,
    FunctionDescriptor,
    ParseResult,
    QualityIssue,
    QualityResult,
    Recommendation,
    SecurityFinding,
    SecurityResult,
)

__all__ = [
    "ParserAgent",
    "SecurityAgent",
    "QualityAgent",
    "ReportAgent",
]


async def _gather_sources(input_path: str) -> dict[Path, str]:
    path = Path(input_path)
    files: Iterable[Path]
    if path.is_file():
        files = [path]
    else:
        files = sorted(p for p in path.rglob("*.py") if p.is_file())
    loop = asyncio.get_running_loop()
    contents: dict[Path, str] = {}
    for file_path in files:
        contents[file_path] = await loop.run_in_executor(None, file_path.read_text)
    return contents


class ParserAgent(BaseAgent):
    async def execute(self, task: AgentTask) -> AgentResult:
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("ParserAgent requires an input_path")
        sources = await _gather_sources(input_path)
        functions: list[FunctionDescriptor | dict[str, Any]] = []
        classes: list[ClassDescriptor | dict[str, Any]] = []
        imports: set[str] = set()
        dependencies: dict[str, str] = {}
        for file_path, content in sources.items():
            module = ast.parse(content or "", filename=str(file_path))
            for node in module.body:
                if isinstance(node, ast.FunctionDef):
                    functions.append(self._build_function_descriptor(node))
                elif isinstance(node, ast.ClassDef):
                    classes.append(self._build_class_descriptor(node))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    module_name = self._extract_import(node)
                    if module_name:
                        imports.add(module_name)
                        dependencies[module_name] = "external"
        parse_result = ParseResult(
            functions=functions,
            classes=classes,
            imports=sorted(imports),
            dependencies=dependencies,
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": parse_result},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _build_function_descriptor(self, node: ast.FunctionDef) -> FunctionDescriptor:
        params = [arg.arg for arg in node.args.args]
        return FunctionDescriptor(
            name=node.name,
            params=params,
            return_type=self._annotation_to_str(node.returns),
            line_range=self._line_range(node),
        )

    def _build_class_descriptor(self, node: ast.ClassDef) -> ClassDescriptor:
        methods = [item.name for item in node.body if isinstance(item, ast.FunctionDef)]
        return ClassDescriptor(name=node.name, line_range=self._line_range(node), methods=methods)

    def _annotation_to_str(self, annotation: ast.AST | None) -> str | None:
        if annotation is None:
            return None
        if isinstance(annotation, ast.Name):
            return annotation.id
        if isinstance(annotation, ast.Attribute):
            return annotation.attr
        return ast.unparse(annotation)

    def _line_range(self, node: ast.AST) -> str:
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        return f"{start}-{end}"

    def _extract_import(self, node: ast.stmt) -> str | None:
        if isinstance(node, ast.Import):
            if node.names:
                return node.names[0].name
        if isinstance(node, ast.ImportFrom):
            return node.module
        return None


class SecurityAgent(BaseAgent):
    async def execute(self, task: AgentTask) -> AgentResult:
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("SecurityAgent requires an input_path")
        sources = await _gather_sources(input_path)
        findings: list[SecurityFinding] = []
        for path, content in sources.items():
            findings.extend(self._scan_content(path, content))
        result = SecurityResult(findings=findings)
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": result},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _scan_content(self, path: Path, content: str) -> list[SecurityFinding]:
        issues: list[SecurityFinding] = []
        lowered = content.lower()
        if "password" in lowered or "secret" in lowered:
            issues.append(
                SecurityFinding(
                    severity="high",
                    location=f"{path.name}:1",
                    description="Possible hardcoded credential detected",
                    recommendation="Load sensitive data from environment variables instead.",
                )
            )
        if "os.system" in lowered or "subprocess" in lowered:
            issues.append(
                SecurityFinding(
                    severity="medium",
                    location=f"{path.name}:1",
                    description="Shell execution detected; ensure inputs are sanitized",
                    recommendation="Use subprocess with explicit arguments and validate inputs.",
                )
            )
        if "eval(" in lowered or "exec(" in lowered:
            issues.append(
                SecurityFinding(
                    severity="critical",
                    location=f"{path.name}:1",
                    description="Dynamic code execution detected",
                    recommendation="Avoid eval/exec with untrusted input.",
                )
            )
        return issues


class QualityAgent(BaseAgent):
    async def execute(self, task: AgentTask) -> AgentResult:
        input_path = task.input_data.get("input_path")
        if not input_path:
            raise ValueError("QualityAgent requires an input_path")
        sources = await _gather_sources(input_path)
        complexity_scores: Counter[str] = Counter()
        issues: list[QualityIssue] = []
        for path, content in sources.items():
            module = ast.parse(content or "", filename=str(path))
            for node in module.body:
                if isinstance(node, ast.FunctionDef):
                    complexity = self._estimate_complexity(node)
                    complexity_scores[node.name] += complexity
                    if complexity > 10:
                        issues.append(
                            QualityIssue(
                                location=node.name,
                                description="Function complexity exceeds recommended threshold",
                                severity="medium",
                            )
                        )
                    length = (getattr(node, "end_lineno", node.lineno) - node.lineno) + 1
                    if length > 50:
                        issues.append(
                            QualityIssue(
                                location=node.name,
                                description="Function exceeds 50 lines; consider refactoring",
                                severity="low",
                            )
                        )
        score = max(0, 100 - len(issues) * 5)
        result = QualityResult(
            score=score,
            issues=issues,
            metrics={"cyclomatic_complexity": dict(complexity_scores)},
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": result},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _estimate_complexity(self, node: ast.FunctionDef) -> int:
        complexity = 1
        for child in ast.walk(node):
            if isinstance(
                child, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.Match)
            ):
                complexity += 1
        return complexity


class ReportAgent(BaseAgent):
    async def execute(self, task: AgentTask) -> AgentResult:
        results_map = task.input_data.get("results") or {}
        parse_result = self._find_result(results_map, ParseResult)
        security_result = self._find_result(results_map, SecurityResult)
        quality_result = self._find_result(results_map, QualityResult)
        recommendations = self._build_recommendations(security_result, quality_result)
        report = AnalysisReport(
            executive_summary=self._build_summary(parse_result, quality_result),
            security_section={
                "findings": [finding.description for finding in security_result.findings]
            },
            quality_section={
                "score": quality_result.score,
                "issues": [issue.description for issue in quality_result.issues],
            },
            recommendations=recommendations,
        )
        return AgentResult(
            task_id=task.task_id,
            agent_id=self.agent_id,
            output_data={"result": report},
            status="success",
            error=None,
            duration_ms=0.0,
            trace_context=task.trace_context or {},
        )

    def _find_result(self, values: dict[str, Any], model_type: type[Any]) -> Any:
        for candidate in values.values():
            if isinstance(candidate, model_type):
                return candidate
            if isinstance(candidate, dict):
                try:
                    return model_type.model_validate(candidate)
                except Exception:  # noqa: BLE001
                    continue
        return model_type()  # type: ignore[call-arg]

    def _build_summary(self, parse_result: ParseResult, quality_result: QualityResult) -> str:
        functions = len(parse_result.functions)
        classes = len(parse_result.classes)
        return f"Analyzed {functions} functions and {classes} classes with quality score {quality_result.score}."

    def _build_recommendations(
        self,
        security_result: SecurityResult,
        quality_result: QualityResult,
    ) -> list[Recommendation]:
        recs: list[Recommendation] = []
        if security_result.findings:
            recs.append(
                Recommendation(
                    title="Address security findings",
                    priority="high",
                    detail="Resolve high and critical issues before deployment.",
                )
            )
        if quality_result.score < 90:
            recs.append(
                Recommendation(
                    title="Improve code quality",
                    priority="medium",
                    detail="Refactor highlighted functions and add tests.",
                )
            )
        if not recs:
            recs.append(
                Recommendation(
                    title="Maintain current standards",
                    priority="low",
                    detail="No critical findings detected.",
                )
            )
        return recs
