#!/usr/bin/env python3
"""Firecrawl Swarm Pipeline

Pipeline stages:
1) Source probe pass
2) Budget/base pass (MiniMax M2.5)
3) Auto-escalate failures to Kimi K2.5
4) Write structured outputs + confidence + provenance to JSON (+ optional Supabase)

Usage:
  python firecrawl_swarm_pipeline.py --input urls.txt --out swarm_pipeline_report.json

Optional Supabase env vars:
  SWARM_SUPABASE_URL
  SWARM_SUPABASE_KEY
Optional table names:
  SWARM_RUNS_TABLE=firecrawl_swarm_runs
  SWARM_ITEMS_TABLE=firecrawl_swarm_items
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BLOCK_PATTERNS = [
    r"access denied",
    r"forbidden",
    r"captcha",
    r"verify you are human",
    r"cloudflare",
    r"akamai",
]
LOGIN_PATTERNS = [r"log in", r"sign in", r"login", r"create account"]

MODEL_BY_PROFILE = {
    "budget": "openrouter/minimax/minimax-m2.5",
    "escalated": "moonshotai/kimi-k2.5",
}


@dataclass
class ItemResult:
    url: str
    stage: str
    model_profile: str
    model_name: str
    success: bool
    access_status: str
    quality: str
    confidence: float
    markdown_len: int
    error: str | None
    provenance: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_urls(path: Path) -> list[str]:
    return [
        ln.strip()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def classify_access(markdown: str) -> str:
    t = (markdown or "").lower()
    if any(re.search(p, t) for p in BLOCK_PATTERNS):
        return "blocked"
    if any(re.search(p, t) for p in LOGIN_PATTERNS):
        return "login-gated"
    return "accessible"


def quality(markdown: str, min_len: int) -> str:
    access = classify_access(markdown)
    if access == "blocked":
        return "blocked"
    if len(markdown or "") < min_len:
        return "low_content"
    return "ok"


def confidence_score(markdown: str, min_len: int) -> float:
    q = quality(markdown, min_len)
    if q == "blocked":
        return 0.0
    if q == "low_content":
        return 0.35

    # heuristic confidence from content volume
    ln = len(markdown or "")
    if ln >= 8000:
        return 0.95
    if ln >= 3000:
        return 0.85
    if ln >= min_len:
        return 0.72
    return 0.4


def run_profile_switch(profile: str, firecrawl_dir: Path) -> None:
    script = Path.home() / ".openclaw" / "skills" / "firecrawl-ops" / "scripts" / "set_model_profile.sh"
    subprocess.run([str(script), profile], check=True)
    subprocess.run(["docker", "compose", "down"], cwd=str(firecrawl_dir), check=True)
    subprocess.run(["docker", "compose", "up", "-d"], cwd=str(firecrawl_dir), check=True)


def scrape(api: str, url: str, timeout: int = 180) -> tuple[bool, str, str | None]:
    try:
        r = requests.post(
            f"{api}/scrape",
            json={"url": url, "formats": ["markdown"]},
            timeout=timeout,
        )
        j = r.json()
        md = (j.get("data") or {}).get("markdown", "")
        ok = bool(j.get("success"))
        err = None if ok else str(j.get("error") or "scrape_failed")
        return ok, md, err
    except Exception as e:
        return False, "", str(e)


def supabase_post(url: str, key: str, table: str, payload: list[dict]) -> None:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = requests.post(
        f"{url.rstrip('/')}/rest/v1/{table}", headers=headers, data=json.dumps(payload), timeout=30
    )
    r.raise_for_status()


def maybe_write_supabase(run_id: str, report: dict[str, Any]) -> tuple[bool, str | None]:
    sb_url = os.getenv("SWARM_SUPABASE_URL")
    sb_key = os.getenv("SWARM_SUPABASE_KEY")
    if not sb_url or not sb_key:
        return False, "supabase_env_not_set"

    runs_table = os.getenv("SWARM_RUNS_TABLE", "firecrawl_swarm_runs")
    items_table = os.getenv("SWARM_ITEMS_TABLE", "firecrawl_swarm_items")

    try:
        run_row = {
            "run_id": run_id,
            "created_at": report["created_at"],
            "summary": report["summary"],
            "config": report["config"],
        }
        supabase_post(sb_url, sb_key, runs_table, [run_row])

        item_rows = []
        for x in report["items"]:
            row = {
                "run_id": run_id,
                "url": x["url"],
                "stage": x["stage"],
                "model_profile": x["model_profile"],
                "model_name": x["model_name"],
                "success": x["success"],
                "access_status": x["access_status"],
                "quality": x["quality"],
                "confidence": x["confidence"],
                "markdown_len": x["markdown_len"],
                "error": x["error"],
                "provenance": x["provenance"],
            }
            item_rows.append(row)

        # chunk inserts
        chunk = 200
        for i in range(0, len(item_rows), chunk):
            supabase_post(sb_url, sb_key, items_table, item_rows[i : i + chunk])

        return True, None
    except Exception as e:
        return False, str(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:3002/v1")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", default="swarm_pipeline_report.json")
    ap.add_argument("--min-len", type=int, default=1200)
    ap.add_argument("--restart-between-stages", action="store_true")
    ap.add_argument("--firecrawl-dir", default=str(Path.home() / "Documents" / "GitHub" / "firecrawl"))
    args = ap.parse_args()

    urls = load_urls(Path(args.input))
    run_id = str(uuid.uuid4())
    created_at = now_iso()

    all_items: list[ItemResult] = []

    def stage_run(stage_name: str, profile: str, target_urls: list[str]) -> list[ItemResult]:
        if args.restart_between_stages:
            run_profile_switch(profile, Path(args.firecrawl_dir))
            time.sleep(2)

        out: list[ItemResult] = []
        for u in target_urls:
            ok, md, err = scrape(args.api, u)
            access = classify_access(md)
            q = quality(md, args.min_len)
            conf = confidence_score(md, args.min_len)
            out.append(
                ItemResult(
                    url=u,
                    stage=stage_name,
                    model_profile=profile,
                    model_name=MODEL_BY_PROFILE[profile],
                    success=ok,
                    access_status=access,
                    quality=q if ok else "error",
                    confidence=conf if ok else 0.0,
                    markdown_len=len(md),
                    error=err,
                    provenance={
                        "timestamp": now_iso(),
                        "endpoint": "/scrape",
                    },
                )
            )
        return out

    # Stage 1: probe + budget combined pass
    s1 = stage_run("probe_budget", "budget", urls)
    all_items.extend(s1)

    kimi_batch = sorted({x.url for x in s1 if x.quality in {"low_content", "error", "blocked"}})

    # Stage 2: kimi escalation
    s2: list[ItemResult] = []
    if kimi_batch:
        s2 = stage_run("escalate_kimi", "escalated", kimi_batch)
        all_items.extend(s2)

    final_by_url: dict[str, ItemResult] = {}
    # precedence: latest stage wins
    for x in all_items:
        final_by_url[x.url] = x

    final_items = list(final_by_url.values())
    summary = {
        "total_urls": len(urls),
        "final_ok": sum(1 for x in final_items if x.quality == "ok"),
        "final_low_content": sum(1 for x in final_items if x.quality == "low_content"),
        "final_blocked": sum(1 for x in final_items if x.quality == "blocked"),
        "final_error": sum(1 for x in final_items if x.quality == "error"),
        "avg_confidence": round(sum(x.confidence for x in final_items) / max(len(final_items), 1), 4),
        "kimi_escalations": len(kimi_batch),
    }

    report = {
        "run_id": run_id,
        "created_at": created_at,
        "config": {
            "api": args.api,
            "min_len": args.min_len,
            "restart_between_stages": args.restart_between_stages,
            "profiles": MODEL_BY_PROFILE,
        },
        "summary": summary,
        "items": [asdict(x) for x in all_items],
        "final_items": [asdict(x) for x in final_items],
    }

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))

    sb_ok, sb_err = maybe_write_supabase(run_id, report)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "summary": summary,
                "report_path": str(Path(args.out).resolve()),
                "supabase_write": "ok" if sb_ok else "skipped_or_failed",
                "supabase_error": sb_err,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
