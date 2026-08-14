#!/usr/bin/env python3
"""Generate a local Firecrawl capability matrix from routes, docs, and smoke results."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_ROUTE_FILE = Path("apps/api/src/routes/v2.ts")
DEFAULT_DOC_FILE = Path("docs/firecrawl-ops/references/tools-capabilities.md")
DEFAULT_SMOKE_DIR = Path("tasks/tmp")
DEFAULT_OUT = Path("docs/firecrawl-ops/references/local-capability-matrix.md")

SMOKE_ROUTE_MAP = {
    "api_root": ("GET", "/"),
    "v2_scrape": ("POST", "/scrape"),
    "v2_map": ("POST", "/map"),
    "v2_search": ("POST", "/search"),
    "v2_parse_pdf_fast": ("POST", "/parse"),
    "v2_batch_scrape": ("POST", "/batch/scrape"),
    "v2_crawl": ("POST", "/crawl"),
    "v2_team_queue_status": ("GET", "/team/queue-status"),
    "v2_crawl_active": ("GET", "/crawl/active"),
    "optional_browser_list": ("GET", "/browser"),
    "optional_browser_create": ("POST", "/browser"),
    "optional_agent_create": ("POST", "/agent"),
    "optional_support_proxy": ("POST", "/support/ask"),
}


@dataclass(frozen=True)
class Route:
    method: str
    path: str


def extract_routes(route_file: Path) -> list[Route]:
    text = route_file.read_text(encoding="utf-8")
    pattern = re.compile(
        r"v2Router\.(get|post|delete|patch|ws)\(\s*(\[[^\]]+\]|\"[^\"]+\")",
        re.MULTILINE,
    )
    routes: list[Route] = []
    seen: set[tuple[str, str]] = set()
    for method, target in pattern.findall(text):
        paths = re.findall(r'"([^"]+)"', target)
        for path in paths:
            key = (method.upper(), path)
            if key in seen:
                continue
            seen.add(key)
            routes.append(Route(method=method.upper(), path=path))
    return sorted(routes, key=lambda item: (item.path, item.method))


def latest_smoke_file(smoke_dir: Path) -> Path | None:
    if not smoke_dir.is_dir():
        return None
    files = list(smoke_dir.rglob("*-local-api-smoke.json"))
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def load_smoke_status(smoke_path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if smoke_path is None:
        return {}
    payload = json.loads(smoke_path.read_text(encoding="utf-8"))
    statuses: dict[tuple[str, str], dict[str, Any]] = {}
    for result in payload.get("results", []):
        route = SMOKE_ROUTE_MAP.get(result.get("name"))
        if route:
            statuses[route] = result
    return statuses


def doc_mentions(doc_text: str, path: str) -> bool:
    route = f"/v2{path}"
    if route in doc_text:
        return True
    if path.startswith("/batch/scrape"):
        return "batch scrape" in doc_text.lower()
    if path.startswith("/team/queue-status") or path.startswith("/crawl/active"):
        return "queue-status" in doc_text or "crawl/active" in doc_text
    return path.strip("/") in doc_text


def classify(route: Route, smoke: dict[tuple[str, str], dict[str, Any]]) -> tuple[str, str]:
    smoke_result = smoke.get((route.method, route.path))
    if smoke_result:
        status = smoke_result.get("status")
        if status == "pass":
            return "works locally", smoke_result.get("detail", "live smoke passed")
        if status == "skip":
            return "needs optional service", smoke_result.get("detail", "skipped by smoke matrix")
        return "not working in latest smoke", smoke_result.get("error") or smoke_result.get("detail") or "smoke failed"

    if route.path.startswith(("/browser", "/interact")):
        return "needs optional service", "requires browser-service configuration"
    if route.path.startswith("/scrape/") and "interact" in route.path:
        return "needs optional service", "requires browser-service or interactive scrape support"
    if route.path.startswith("/agent"):
        return "needs optional service", "requires EXTRACT_V3_BETA_URL"
    if route.path.startswith("/support"):
        return "needs optional service", "requires SUPPORT_AGENT_URL"
    if route.path.startswith(("/research", "/search/research")):
        return "needs optional service", "requires RESEARCH_PROXY_URL"
    if route.path.startswith("/x402"):
        return "hosted or configured only", "requires x402 payment configuration"
    if route.path.startswith("/monitor"):
        return "hosted or configured only", "monitor backend is not part of the default local ops stack"
    if route.path.startswith("/extract"):
        return "needs model env", "deprecated v2 extract path requires schema and model provider env"
    if route.path == "/crawl/params-preview":
        return "needs model env", "LLM-backed crawl option generation"
    if route.path.startswith("/team/credit-usage") or route.path.startswith("/team/token-usage"):
        return "not tested", "accounting route registered but not in local smoke matrix"
    if route.path in {"/feedback", "/search/:jobId/feedback"}:
        return "not tested", "feedback route registered but not in local smoke matrix"
    if route.path in {"/keyless/eligibility", "/team/activity", "/concurrency-check"}:
        return "not tested", "diagnostic route registered but not in local smoke matrix"
    if route.path.startswith("/crawl/") or route.path.startswith("/batch/scrape/") or route.path.startswith("/scrape/"):
        return "partly covered", "base async workflow is covered, this status/error/cancel variant is not directly probed"
    return "not tested", "registered route is not covered by the latest local smoke matrix"


def matrix_rows(routes: list[Route], doc_text: str, smoke: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for route in routes:
        status, note = classify(route, smoke)
        rows.append(
            {
                "method": route.method,
                "path": route.path,
                "status": status,
                "documented": "yes" if doc_mentions(doc_text, route.path) else "no",
                "note": note,
            }
        )
    return rows


def write_markdown(
    rows: list[dict[str, str]],
    out: Path,
    route_file: Path,
    doc_file: Path,
    smoke_path: Path | None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "# Local Firecrawl Capability Matrix",
        "",
        f"Generated: `{stamp}`",
        f"Route source: `{route_file}`",
        f"Reference source: `{doc_file}`",
        f"Smoke source: `{smoke_path}`" if smoke_path else "Smoke source: `none found`",
        "",
        "| Method | Route | Local status | In ops docs | Notes |",
        "|---|---|---|---:|---|",
    ]
    for row in rows:
        note = row["note"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{row['method']}` | `/v2{row['path']}` | `{row['status']}` | `{row['documented']}` | {note} |"
        )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route-file", type=Path, default=DEFAULT_ROUTE_FILE)
    parser.add_argument("--doc-file", type=Path, default=DEFAULT_DOC_FILE)
    parser.add_argument("--smoke-file", type=Path)
    parser.add_argument("--smoke-dir", type=Path, default=DEFAULT_SMOKE_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    smoke_path = args.smoke_file or latest_smoke_file(args.smoke_dir)
    if smoke_path is None:
        print(
            "No local API smoke artifact found. Run local_api_smoke_matrix.py first "
            "or pass --smoke-file PATH.",
            file=sys.stderr,
        )
        return 2
    routes = extract_routes(args.route_file)
    doc_text = args.doc_file.read_text(encoding="utf-8") if args.doc_file.is_file() else ""
    smoke = load_smoke_status(smoke_path)
    rows = matrix_rows(routes, doc_text, smoke)
    write_markdown(rows, args.out, args.route_file, args.doc_file, smoke_path)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
