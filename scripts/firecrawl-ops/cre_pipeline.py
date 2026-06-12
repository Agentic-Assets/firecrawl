#!/usr/bin/env python3
"""CRE Listing Pipeline CLI

Usage:
  python3 cre_pipeline.py run-all [--max=100] [--out=./output] [--broker=<slug>] ...
  python3 cre_pipeline.py run <broker> [--max=50] [--out=./output]
  python3 cre_pipeline.py status [--out=./output]
  python3 cre_pipeline.py export [--out=./cre_listings.jsonl] [--broker=<slug>] [--dir=./output]
  python3 cre_pipeline.py apply-schema [--dry-run] [--file=<sql_file>]

Brokers: cbre, jll, cushman-wakefield, colliers, marcus-millichap,
         avison-young, svn, nai-global, newmark, lee-associates

Environment variables:
  FIRECRAWL_API_URL      Default: http://localhost:3002
  FIRECRAWL_API_KEY      Optional bearer token
  SUPABASE_URL           Supabase project URL (enables DB persistence)
  SUPABASE_SERVICE_KEY   Supabase service role key
  DATABASE_URL           Postgres connection string (enables psql for apply-schema)

Examples:
  # Run all active brokers, max 100 listings each
  python3 cre_pipeline.py run-all --max=100 --out=./output

  # Run just CBRE and Colliers
  python3 cre_pipeline.py run-all --broker=cbre --broker=colliers --max=50

  # Run single broker
  python3 cre_pipeline.py run colliers --max=25

  # Check checkpoint status for all brokers
  python3 cre_pipeline.py status

  # Export all scraped listings to JSONL
  python3 cre_pipeline.py export --out=./cre_listings.jsonl

  # Apply database schema (requires DATABASE_URL or SUPABASE_URL + SUPABASE_SERVICE_KEY)
  python3 cre_pipeline.py apply-schema
  python3 cre_pipeline.py apply-schema --dry-run
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure cre_scrapers package is importable from this script's location.
# The script lives alongside the cre_scrapers/ package directory.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Argument parsing (stdlib only -- no argparse for portability)
# ---------------------------------------------------------------------------

def _parse_flag(args: list[str], name: str) -> bool:
    """Return True if --name is present in args."""
    return f"--{name}" in args


def _parse_opt(args: list[str], name: str, default: str | None = None) -> str | None:
    """Return the value of --name=VALUE or --name VALUE from args list."""
    prefix = f"--{name}="
    for i, arg in enumerate(args):
        if arg.startswith(prefix):
            return arg[len(prefix):]
        if arg == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return default


def _parse_opt_list(args: list[str], name: str) -> list[str]:
    """Collect all --name=VALUE occurrences (repeated flag)."""
    prefix = f"--{name}="
    values = []
    for i, arg in enumerate(args):
        if arg.startswith(prefix):
            values.append(arg[len(prefix):])
        elif arg == f"--{name}" and i + 1 < len(args):
            values.append(args[i + 1])
    return values


# ---------------------------------------------------------------------------
# Schema application helpers
# ---------------------------------------------------------------------------

def _apply_schema_via_psql(sql_dir: Path, database_url: str, dry_run: bool) -> int:
    """Apply 000_run_all.sql using psql. Returns exit code."""
    run_all = sql_dir / "000_run_all.sql"
    if not run_all.exists():
        print(f"[apply-schema] ERROR: {run_all} not found", file=sys.stderr)
        return 1
    if dry_run:
        print(f"[apply-schema] DRY RUN: would execute: psql ... -f {run_all}")
        return 0
    cmd = ["psql", database_url, "-v", "ON_ERROR_STOP=1", "-f", str(run_all)]
    print(f"[apply-schema] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(sql_dir))
    return result.returncode


def _apply_schema_via_supabase_rest(
    sql_dir: Path, supabase_url: str, service_key: str, dry_run: bool, specific_file: str | None
) -> int:
    """Apply SQL migrations via Supabase REST API (exec_sql RPC or direct SQL endpoint).

    Supabase does not expose a generic exec_sql RPC by default; we use the
    /rest/v1/rpc/exec_sql pattern if available. If not, prints instructions
    for manual application via the SQL editor.
    """
    if specific_file:
        sql_files = [sql_dir / specific_file]
    else:
        # Apply in dependency order
        sql_files = sorted(sql_dir.glob("[0-9]*.sql"))
        # Exclude 000_run_all.sql (it uses \i psql meta-commands not valid over REST)
        sql_files = [f for f in sql_files if f.name != "000_run_all.sql"]

    if not sql_files:
        print(f"[apply-schema] No SQL files found in {sql_dir}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[apply-schema] DRY RUN: would apply {len(sql_files)} file(s):")
        for f in sql_files:
            print(f"  {f.name}")
        return 0

    headers = {
        "Content-Type": "application/json",
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }

    errors = 0
    for sql_path in sql_files:
        sql_content = sql_path.read_text()
        print(f"[apply-schema] Applying {sql_path.name} ({len(sql_content)} chars) ...")

        # Try /rest/v1/rpc/exec_sql
        rpc_url = f"{supabase_url.rstrip('/')}/rest/v1/rpc/exec_sql"
        body = json.dumps({"sql": sql_content}).encode()
        req = urllib.request.Request(rpc_url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
            print(f"[apply-schema] {sql_path.name}: OK")
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if e.code == 404:
                # exec_sql RPC not available; print manual instructions
                print(
                    f"[apply-schema] WARN: exec_sql RPC not available (HTTP 404).\n"
                    f"  Apply manually via the Supabase SQL Editor:\n"
                    f"  {supabase_url}/project/_/sql\n"
                    f"  Paste the contents of: {sql_path}",
                    file=sys.stderr,
                )
                errors += 1
            else:
                print(
                    f"[apply-schema] ERROR: {sql_path.name}: HTTP {e.code}: {raw[:200]}",
                    file=sys.stderr,
                )
                errors += 1

    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# CLI command implementations
# ---------------------------------------------------------------------------

def cmd_run_all(args: list[str]) -> int:
    """Run all active brokers (or a specified subset)."""
    from cre_scrapers.pipeline import CREScrapingPipeline

    max_per = _parse_opt(args, "max")
    out_dir = _parse_opt(args, "out", "./output")
    broker_slugs = _parse_opt_list(args, "broker") or None
    verbose = _parse_flag(args, "verbose")
    _setup_logging(verbose)

    max_listings = int(max_per) if max_per else None

    print(f"[run-all] firecrawl={os.environ.get('FIRECRAWL_API_URL', 'http://localhost:3002')}")
    print(f"[run-all] output={out_dir}  max_per_broker={max_listings or 'unlimited'}")
    if broker_slugs:
        print(f"[run-all] brokers={', '.join(broker_slugs)}")
    else:
        print("[run-all] brokers=all active")

    pipeline = CREScrapingPipeline()
    try:
        stats = pipeline.run_all(
            broker_slugs=broker_slugs,
            max_per_broker=max_listings,
            output_dir=out_dir,
        )
    except ValueError as exc:
        print(f"[run-all] ERROR: {exc}", file=sys.stderr)
        return 1

    print("\n=== run-all summary ===")
    print(f"  discovered : {stats['total_discovered']}")
    print(f"  scraped    : {stats['total_scraped']}")
    print(f"  saved      : {stats['total_saved']}")
    print(f"  errors     : {stats['total_errors']}")
    print(f"  duration   : {stats['duration_s']}s")
    print()
    print("  per-broker:")
    for slug, s in stats.get("brokers", {}).items():
        if s.get("skipped"):
            print(f"    {slug:22s}  SKIPPED ({s.get('reason', '')})")
        elif s.get("error"):
            print(f"    {slug:22s}  ERROR: {s['error']}")
        else:
            print(
                f"    {slug:22s}  disc={s.get('discovered',0):4d}  "
                f"scraped={s.get('scraped',0):4d}  saved={s.get('saved',0):4d}  "
                f"err={s.get('errors',0):2d}  {s.get('duration_s',0):.0f}s"
            )
    return 0


def cmd_run(args: list[str]) -> int:
    """Run the pipeline for a single broker."""
    from cre_scrapers.pipeline import CREScrapingPipeline

    if not args or args[0].startswith("--"):
        print("Usage: run <broker_slug> [--max=N] [--out=./output]", file=sys.stderr)
        return 1

    slug = args[0]
    rest = args[1:]
    max_listings_str = _parse_opt(rest, "max")
    out_dir = _parse_opt(rest, "out", "./output")
    verbose = _parse_flag(rest, "verbose")
    _setup_logging(verbose)

    max_listings = int(max_listings_str) if max_listings_str else None

    print(f"[run] broker={slug}  max={max_listings or 'unlimited'}  out={out_dir}")

    pipeline = CREScrapingPipeline()
    try:
        stats = pipeline.run_broker(slug, max_listings=max_listings, output_dir=out_dir)
    except KeyError as exc:
        print(f"[run] ERROR: {exc}", file=sys.stderr)
        return 1

    print("\n=== run summary ===")
    for key, val in stats.items():
        print(f"  {key:16s} : {val}")
    return 0


def cmd_status(args: list[str]) -> int:
    """Print checkpoint status for all brokers."""
    from cre_scrapers.pipeline import CREScrapingPipeline

    out_dir = _parse_opt(args, "out", "./output")
    verbose = _parse_flag(args, "verbose")
    _setup_logging(verbose)

    pipeline = CREScrapingPipeline()
    status = pipeline.get_status(output_dir=out_dir)

    print(f"{'slug':22s}  {'active':6s}  {'disc':>6}  {'scraped':>7}  {'saved':>5}  {'err':>4}  last_run")
    print("-" * 90)
    for slug, s in sorted(status.items()):
        active_flag = "yes" if s.get("active") else "NO"
        last = s.get("last_run") or "-"
        if last != "-":
            last = last[:19].replace("T", " ")
        if not s.get("checkpoint_exists"):
            last = "(no checkpoint)"
        print(
            f"  {slug:20s}  {active_flag:6s}  {s['discovered']:>6}  "
            f"{s['scraped']:>7}  {s['saved']:>5}  {s['errors']:>4}  {last}"
        )
    return 0


def cmd_export(args: list[str]) -> int:
    """Export scraped listings to a JSONL file."""
    from cre_scrapers.pipeline import CREScrapingPipeline

    out_path = _parse_opt(args, "out", "./cre_listings.jsonl")
    broker_slugs = _parse_opt_list(args, "broker") or None
    data_dir = _parse_opt(args, "dir", "./output")
    verbose = _parse_flag(args, "verbose")
    _setup_logging(verbose)

    print(f"[export] output={out_path}  data_dir={data_dir}")
    if broker_slugs:
        print(f"[export] brokers={', '.join(broker_slugs)}")

    pipeline = CREScrapingPipeline()
    count = pipeline.export_jsonl(out_path, broker_slugs=broker_slugs, output_dir=data_dir)
    print(f"[export] wrote {count} records -> {out_path}")
    return 0


def cmd_apply_schema(args: list[str]) -> int:
    """Apply the CRE listing SQL schema to the configured database."""
    dry_run = _parse_flag(args, "dry-run")
    specific_file = _parse_opt(args, "file")
    verbose = _parse_flag(args, "verbose")
    _setup_logging(verbose)

    sql_dir = _SCRIPT_DIR / "sql"
    if not sql_dir.is_dir():
        print(f"[apply-schema] ERROR: sql/ directory not found at {sql_dir}", file=sys.stderr)
        return 1

    database_url = os.environ.get("DATABASE_URL", "")
    supabase_url = os.environ.get("SUPABASE_URL", "")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    if database_url:
        print(f"[apply-schema] Using psql with DATABASE_URL")
        return _apply_schema_via_psql(sql_dir, database_url, dry_run)

    if supabase_url and service_key:
        print(f"[apply-schema] Using Supabase REST API at {supabase_url}")
        return _apply_schema_via_supabase_rest(
            sql_dir, supabase_url, service_key, dry_run, specific_file
        )

    # No credentials: print manual instructions
    print(
        "[apply-schema] No database credentials found.\n"
        "\n"
        "To apply the schema, set one of:\n"
        "\n"
        "  Option A (psql):\n"
        "    export DATABASE_URL='postgresql://postgres:<pwd>@db.<project>.supabase.co:5432/postgres'\n"
        "    python3 cre_pipeline.py apply-schema\n"
        "\n"
        "  Option B (Supabase REST):\n"
        "    export SUPABASE_URL='https://<project>.supabase.co'\n"
        "    export SUPABASE_SERVICE_KEY='<service_role_key>'\n"
        "    python3 cre_pipeline.py apply-schema\n"
        "\n"
        "  Option C (Supabase SQL Editor):\n"
        f"    Paste the contents of each file in {sql_dir}/ in order:\n"
        "    001_cre_brokerages.sql -> 002_cre_listings.sql -> "
        "003_cre_scrape_tracking.sql -> 004_cre_indexes.sql -> 005_cre_views.sql\n",
        file=sys.stderr,
    )
    return 1


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

COMMANDS: dict[str, tuple] = {
    "run-all":      (cmd_run_all,      "Run all active brokers"),
    "run":          (cmd_run,          "Run a single broker"),
    "status":       (cmd_status,       "Show checkpoint status for all brokers"),
    "export":       (cmd_export,       "Export scraped listings to JSONL"),
    "apply-schema": (cmd_apply_schema, "Apply the CRE listing SQL schema"),
}


def _usage() -> None:
    print(__doc__.strip())
    print("\nAvailable commands:")
    for name, (_, desc) in COMMANDS.items():
        print(f"  {name:14s}  {desc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help", "help"):
        _usage()
        return 0

    cmd_name = argv[0]
    cmd_args = argv[1:]

    if cmd_name not in COMMANDS:
        print(f"Unknown command: {cmd_name}", file=sys.stderr)
        print(f"Available: {', '.join(COMMANDS)}", file=sys.stderr)
        return 1

    fn, _ = COMMANDS[cmd_name]
    try:
        return fn(cmd_args)
    except KeyboardInterrupt:
        print("\n[cre_pipeline] interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.getLogger(__name__).exception("Unhandled error in %s", cmd_name)
        print(f"[cre_pipeline] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
