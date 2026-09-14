#!/usr/bin/env python3
"""Scrape one stored CRE listing URL per brokerage with one plain v2 request.

This is intentionally a baseline, not the governed CRE collector. Every target
uses the same Firecrawl request: ``POST /v2/scrape`` with Markdown only and no
broker-specific proxy, cookie, action, render, or extraction settings.

Examples:

    python3 scripts/firecrawl-ops/scrape_cre_listing_baseline.py \
      --api-url http://localhost:3102

    python3 scripts/firecrawl-ops/scrape_cre_listing_baseline.py \
      --input scripts/firecrawl-ops/cre-brokerage-example-listing-urls.json \
      --out-dir tasks/tmp/cre-brokerage-scrape-baseline \
      --api-url http://localhost:3102
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    REPO_ROOT / "scripts/firecrawl-ops/cre-brokerage-example-listing-urls.json"
)
DEFAULT_OUTPUT = REPO_ROOT / "tasks/tmp/cre-brokerage-scrape-baseline"
DEFAULT_API_URL = "http://localhost:3002"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "brokerage"


def load_targets(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Target file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Target file is not valid JSON: {path}: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise SystemExit("Target file must contain a non-empty JSON array.")

    targets: list[dict[str, str]] = []
    seen_brokerages: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"Target {index} must be an object.")
        brokerage = item.get("brokerage")
        listing_url = item.get("listing_url")
        if not isinstance(brokerage, str) or not brokerage.strip():
            raise SystemExit(f"Target {index} has no non-empty brokerage name.")
        if not isinstance(listing_url, str) or not re.match(r"^https?://", listing_url):
            raise SystemExit(f"Target {index} has no HTTP(S) listing_url.")
        if brokerage in seen_brokerages:
            raise SystemExit(f"Target file repeats brokerage {brokerage!r}.")
        seen_brokerages.add(brokerage)
        targets.append({"brokerage": brokerage, "listing_url": listing_url})
    return targets


def scrape(
    api_url: str, listing_url: str, timeout_seconds: float
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(
        {
            "url": listing_url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
    ).encode("utf-8")
    request = Request(
        f"{api_url.rstrip('/')}/v2/scrape",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read()
            status_code = response.status
    except HTTPError as exc:
        raw_body = exc.read()
        status_code = exc.code
    except URLError as exc:
        return 0, {"error": f"transport error: {exc.reason}"}
    except OSError as exc:
        return 0, {"error": f"transport error: {exc}"}

    try:
        response_body = json.loads(raw_body)
    except json.JSONDecodeError:
        return status_code, {"error": "Firecrawl returned a non-JSON response."}
    if not isinstance(response_body, dict):
        return status_code, {"error": "Firecrawl returned a non-object JSON response."}
    return status_code, response_body


def markdown_document(
    *,
    brokerage: str,
    listing_url: str,
    retrieved_at: str,
    outcome: str,
    http_status: int,
    markdown: str | None,
    title: str | None,
    error: str | None,
) -> str:
    frontmatter = [
        "---",
        f"brokerage: {json.dumps(brokerage)}",
        f"listing_url: {json.dumps(listing_url)}",
        f"retrieved_at: {retrieved_at}",
        f"outcome: {outcome}",
        f"http_status: {http_status if http_status else 'null'}",
    ]
    if title:
        frontmatter.append(f"title: {json.dumps(title)}")
    frontmatter.append("---")
    if outcome == "success":
        return "\n".join(frontmatter) + "\n\n" + (markdown or "").rstrip() + "\n"
    return (
        "\n".join(frontmatter)
        + f"\n\n# Firecrawl scrape failed\n\n{error or 'No error detail returned.'}\n"
    )


def run(args: argparse.Namespace) -> int:
    targets = load_targets(args.input)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    for index, target in enumerate(targets, start=1):
        brokerage = target["brokerage"]
        listing_url = target["listing_url"]
        retrieved_at = utc_now()
        http_status, response = scrape(args.api_url, listing_url, args.timeout)
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        markdown = (
            data.get("markdown") if isinstance(data.get("markdown"), str) else None
        )
        metadata = (
            data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        )
        title = (
            metadata.get("title") if isinstance(metadata.get("title"), str) else None
        )
        error_value = response.get("error") or response.get("message")
        error = error_value if isinstance(error_value, str) else None
        outcome = (
            "success" if response.get("success") is True and markdown else "failed"
        )
        if outcome == "failed" and error is None:
            error = "Firecrawl returned no Markdown content."

        output_path = args.out_dir / f"{index:02d}-{slugify(brokerage)}.md"
        output_path.write_text(
            markdown_document(
                brokerage=brokerage,
                listing_url=listing_url,
                retrieved_at=retrieved_at,
                outcome=outcome,
                http_status=http_status,
                markdown=markdown,
                title=title,
                error=error,
            ),
            encoding="utf-8",
        )
        manifest.append(
            {
                "brokerage": brokerage,
                "listing_url": listing_url,
                "output_file": output_path.name,
                "retrieved_at": retrieved_at,
                "outcome": outcome,
                "http_status": http_status or None,
                "markdown_characters": len(markdown or ""),
                "title": title,
                "error": error,
            }
        )
        print(f"[{index}/{len(targets)}] {brokerage}: {outcome}", flush=True)

    (args.out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    successful = sum(item["outcome"] == "success" for item in manifest)
    print(
        f"Finished: {successful}/{len(manifest)} returned Markdown. Output: {args.out_dir}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("FIRECRAWL_API_URL", DEFAULT_API_URL),
        help="Local Firecrawl API base URL (default: FIRECRAWL_API_URL or localhost:3002).",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
