"""Write local Firecrawl healthcheck evidence from the caller environment."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def main() -> None:
    payload = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": os.environ["STATUS"],
        "api_url": os.environ["API_URL"],
        "firecrawl_dir": os.environ["FC_DIR"],
        "image_id": os.environ.get("IMAGE_ID", ""),
        "errors": json.loads(os.environ.get("ERRORS_JSON", "[]")),
        "docker_compose_ps": os.environ.get("DOCKER_PS", ""),
        "api_root_response": os.environ.get("ROOT_RESP", ""),
        "scrape_response": parse_json(os.environ.get("RESP", "")),
        "scrape_summary": parse_json(os.environ.get("SCRAPE_SUMMARY", "")),
    }

    json_path = Path(os.environ["JSON_PATH"])
    md_path = Path(os.environ["MD_PATH"])
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    lines = [
        "# Firecrawl Healthcheck Evidence",
        "",
        f"- Timestamp: `{payload['timestamp']}`",
        f"- Status: `{payload['status']}`",
        f"- API URL: `{payload['api_url']}`",
        f"- Firecrawl dir: `{payload['firecrawl_dir']}`",
        f"- Image id: `{payload['image_id']}`",
        f"- Errors: `{len(payload['errors'])}`",
        "",
        "## Scrape Summary",
        "",
        "```json",
        json.dumps(payload["scrape_summary"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Docker Compose",
        "",
        "```text",
        payload["docker_compose_ps"],
        "```",
        "",
    ]
    if payload["errors"]:
        lines.extend(["## Errors", ""])
        for error in payload["errors"]:
            lines.append(f"- {error}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
