#!/usr/bin/env python3
"""Transient psql wrapper for the 007 change-tracking apply.

Reuses cre_ingest.load_db_url / find_psql so the connection string is resolved
exactly like the production ingestor and is NEVER printed. Forwards all CLI args
to psql after the (hidden) URL. Example:

    python3 cre_psql.py -q -v ON_ERROR_STOP=1 -c "SELECT 1;"
    python3 cre_psql.py -q -v ON_ERROR_STOP=1 -f /path/to/apply.sql
"""
import os
import subprocess
import sys

CRE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "scripts", "firecrawl-ops", "cre_collector",
)
sys.path.insert(0, os.path.abspath(CRE))

from cre_ingest import load_db_url, find_psql  # noqa: E402

url, env_path = load_db_url(None)  # never printed
psql = find_psql()
print(f"[cre_psql] using env file: {env_path}", file=sys.stderr)
print(f"[cre_psql] psql: {psql}", file=sys.stderr)

proc = subprocess.run([psql, url, *sys.argv[1:]])
sys.exit(proc.returncode)
