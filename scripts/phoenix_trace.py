"""Fetch a trace from the local Phoenix GraphQL API and print it.

Usage:
    uv run python scripts/phoenix_trace.py <trace_id> [--project NAME] [--raw]

Env:
    PHOENIX_URL   default http://localhost:6006

Examples:
    uv run python scripts/phoenix_trace.py 7a920ef5e869ffdaf6f1df05ac99a862
    uv run python scripts/phoenix_trace.py 7a920ef5e869ffdaf6f1df05ac99a862 --raw

The script talks only to /graphql; no browser / Playwright required.
It is intentionally standalone — uses only stdlib + `requests` (already
an indirect dependency via traceloop).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import urllib.request


PHOENIX_URL = os.getenv("PHOENIX_URL", "http://localhost:6006")


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        f"{PHOENIX_URL}/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read())
    if "errors" in payload and payload["errors"]:
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


def list_projects() -> list[dict[str, Any]]:
    data = gql("{ projects(first:50){ edges { node { id name traceCount } } } }")
    return [e["node"] for e in data["projects"]["edges"]]


def find_project_with_trace(trace_id: str) -> dict[str, Any] | None:
    """Walk all projects until one contains the trace."""
    for project in list_projects():
        try:
            data = gql(
                'query($pid:ID!,$tid:ID!){ node(id:$pid){ ... on Project { '
                "trace(traceId:$tid){ traceId latencyMs numSpans } } } }",
                {"pid": project["id"], "tid": trace_id},
            )
            if data["node"] and data["node"].get("trace"):
                return {**project, "trace": data["node"]["trace"]}
        except RuntimeError:
            continue
    return None


SPAN_FIELDS = (
    "spanId parentId name spanKind statusCode statusMessage "
    "startTime endTime latencyMs "
    "tokenCountTotal tokenCountPrompt tokenCountCompletion "
    "input { value mimeType } output { value mimeType }"
)


def fetch_spans(project_id: str, trace_id: str) -> list[dict[str, Any]]:
    query = (
        "query($pid:ID!,$tid:ID!){ node(id:$pid){ ... on Project { "
        f"trace(traceId:$tid){{ spans(first:100){{ edges {{ node {{ {SPAN_FIELDS} }} }} }} }} }} }} }}"
    )
    data = gql(query, {"pid": project_id, "tid": trace_id})
    edges = data["node"]["trace"]["spans"]["edges"]
    return [e["node"] for e in edges]


def _indent_tree(spans: list[dict[str, Any]]) -> str:
    """Return the span list as a readable tree ordered by start time."""
    by_id = {s["spanId"]: s for s in spans}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for s in spans:
        children.setdefault(s.get("parentId"), []).append(s)
    for bucket in children.values():
        bucket.sort(key=lambda n: n["startTime"])

    roots = [s for s in spans if s.get("parentId") not in by_id]
    roots.sort(key=lambda n: n["startTime"])

    lines: list[str] = []

    def emit(span: dict[str, Any], depth: int) -> None:
        prefix = "  " * depth + ("`- " if depth else "")
        lat = span.get("latencyMs") or 0
        status = span.get("statusCode") or ""
        lines.append(f"{prefix}{span['name']}  {lat:.0f} ms  [{status}]")
        for child in children.get(span["spanId"], []):
            emit(child, depth + 1)

    for root in roots:
        emit(root, 0)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_id")
    parser.add_argument("--project", help="Project name (skip auto-discovery)")
    parser.add_argument("--raw", action="store_true", help="Dump raw span JSON")
    parser.add_argument("--list-projects", action="store_true")
    args = parser.parse_args()

    if args.list_projects:
        for p in list_projects():
            print(f"{p['name']:40s}  traces={p['traceCount']}  id={p['id']}")
        return 0

    if args.project:
        project = next((p for p in list_projects() if p["name"] == args.project), None)
        if project is None:
            print(f"project {args.project!r} not found", file=sys.stderr)
            return 2
    else:
        project = find_project_with_trace(args.trace_id)
        if project is None:
            print(f"trace {args.trace_id} not found in any project", file=sys.stderr)
            return 2

    spans = fetch_spans(project["id"], args.trace_id)

    if args.raw:
        print(json.dumps(spans, indent=2))
        return 0

    print(f"Project: {project['name']}")
    print(f"Trace:   {args.trace_id}")
    print(f"Spans:   {len(spans)}\n")
    print(_indent_tree(spans))
    print()
    for s in spans:
        if s.get("spanKind") != "LLM" and "llm" not in s["name"].lower():
            continue
        prompt_tokens = s.get("tokenCountPrompt") or 0
        comp_tokens = s.get("tokenCountCompletion") or 0
        out = (s.get("output") or {}).get("value") or ""
        preview = out[:160].replace("\n", " ")
        print(
            f"  LLM span {s['name']!r}: prompt_tokens={prompt_tokens} "
            f"completion_tokens={comp_tokens} status={s.get('statusCode')}"
        )
        if preview:
            print(f"    output> {preview}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
