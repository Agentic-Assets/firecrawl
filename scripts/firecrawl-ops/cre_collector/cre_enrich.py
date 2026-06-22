#!/usr/bin/env python3
"""
cre_enrich.py: Tier-B enrichment-queue worker (drains credeals.cre_enrichment_queue).

This is the worker the monitor's enqueue path was always feeding. The monitor
(`cre_monitor.py --apply`) enqueues new/changed listings into
credeals.cre_enrichment_queue; nothing previously drained it, so the only thing
that refreshed listing DETAIL was the nightly full re-scrape of everything. This
worker closes that loop: it claims a batch from the queue, runs
`collect.ts --enrich-input` to render ONLY the claimed listings' detail pages,
then ingests the result ADDITIVELY via `cre_ingest.py --in` (no soft-delete, no
status activation), and finally deletes the rows it completed so a LATER change
to the same listing can re-enqueue.

Conventions mirror cre_monitor.py / cre_ingest.py exactly:
  - argparse with --batch (default 200), --env-file, --dry-run.
  - env-file discovery reuses cre_ingest.load_db_url precedence
    (--env-file > CRE_ENV_FILE > ~/Documents defaults). The DB url is NEVER
    printed (only the env-file path is) and never persisted to an artifact.
  - all DB writes are built with sql_lit quote-doubling under
    `SET LOCAL standard_conforming_strings = on` and `-v ON_ERROR_STOP=1`,
    exactly like cre_monitor.build_write_sql. Scraped url text is never
    f-string-interpolated into SQL.
  - Python stdlib only; talks to Postgres via psql (cre_ingest.find_psql).

Structure: PURE BUILDERS (string/argv/partition functions, asserted by the
unit tests with no DB) plus a thin run() that wires claim -> collect -> ingest
-> complete.

Safety model (additive by construction; see ENRICHMENT_WORKER_DESIGN_2026-06-15.md
Section 4 and out/enrich/IMPL_SPEC.md Section 4):
  - The ingest call is ALWAYS exactly ["--in", enriched.json]. It NEVER passes
    --mark-missing / --no-mark-missing (not a cre_ingest.py flag) /
    --activate-status. The worker cannot soft-delete or flip board state.
  - attempts is NOT incremented at claim time (a single stack-down run would
    otherwise burn an attempt for the whole batch and dead-letter healthy rows).
    It is incremented ONLY on claimed-but-absent rows AFTER a successful collect.
  - A whole-run failure (collect rc != 0, or enriched.json missing / invalid /
    empty) releases the claims (claimed_at = NULL, attempts untouched), sets
    last_error, and exits nonzero. It does NOT ingest a partial/empty artifact.
  - Completion is matched by URL (verbatim in both the queue and the artifact),
    NOT by external_id (the queue stores the folded/prefixed id, the artifact
    carries the native source id).
  - The worker writes NO verdict marker; cre_run_tier.sh owns
    out/daily/last_run_enrich.json. The worker communicates only via exit code
    (0 = success or empty-queue no-op; nonzero = collect/ingest failure).

OM-parse step (opt-in, guarded; Phase-2 data-lift WS2):
  After the additive detail re-ingest, an OPT-IN OM-parse step can drain the
  just-enriched listings' parseable OM/brochure PDFs through om_parse.py, which
  re-ingests underwriting scalars (COALESCE-keep) + cre_listing_om_facts
  provenance rows ADDITIVELY. It is OFF by default (the existing enrich flow is
  byte-identical without it) and enabled only by --om-parse or CRE_OM_PARSE=1.
  The OM step shells out to om_parse.py with the SAME additive guarantees as the
  detail ingest: it can never soft-delete or activate status (om_parse re-ingests
  with argv strictly ["--in", path]). A failure in the OM step is logged and does
  NOT fail the enrich run (the detail enrich already succeeded and committed).

Usage:
  python3 cre_enrich.py                      # claim <=200, enrich, ingest, complete
  python3 cre_enrich.py --batch 50
  python3 cre_enrich.py --dry-run            # build claim SQL, print it, do not connect
  python3 cre_enrich.py --om-parse           # also run the opt-in OM-parse step
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# Reuse cre_ingest verbatim so env-file discovery, SQL escaping, and psql
# discovery are identical to the production ingest path. The worker imports these
# symbols; it never re-implements credential loading or quote-doubling.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cre_ingest import (  # noqa: E402
    find_psql,
    load_db_url,
    sql_lit,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ENRICH_DIR = os.path.join(HERE, "out", "enrich")

# Rows whose attempts reach this value fall out of the drain set (attempts < 5)
# and surface in credeals.v_cre_enrichment_dead (sql/010). Mirrors the claim SQL
# `attempts < 5` predicate and select_done_and_retry's dead-letter threshold.
MAX_ATTEMPTS = 5

# A crashed prior run leaves rows claimed (claimed_at set, done_at NULL). The
# claim SQL reclaims any row whose claim is older than this interval. It is a SQL
# literal, never f-string'd from user input.
RECLAIM_INTERVAL = "1 hour"


# ---------------------------------------------------------------------------
# Pure builders (no DB; the unit tests assert on these strings/lists/sets)
# ---------------------------------------------------------------------------


def _lit_or_null(v):
    """sql_lit(v) or the bare NULL literal, mirroring cre_monitor._sql_text."""
    return "NULL" if v is None else sql_lit(v)


def build_claim_sql(batch, *, reclaim_interval=RECLAIM_INTERVAL):
    """Atomically claim up to `batch` pending rows and return their fields.

    Mirrors cre_monitor.build_write_sql GUC pins (ON_ERROR_STOP +
    standard_conforming_strings). `batch` is validated to an int and inlined as a
    bare integer; `reclaim_interval` is a fixed SQL literal, never f-string'd from
    scraped/user input.

    The FOR UPDATE SKIP LOCKED row locks taken in the CTE are held across the
    outer UPDATE ... FROM claimed (single statement), so concurrent workers never
    claim the same row even though the mkdir tier lock already serializes tiers.

    NOTE: attempts is NOT incremented here (incrementing at claim time would let a
    single stack-down run burn an attempt for the whole batch and dead-letter
    healthy rows after five systemic outages). attempts is incremented only on
    claimed-but-absent rows after a successful collect (build_retry_increment_sql).
    """
    batch = int(batch)
    return "\n".join([
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
        "WITH claimed AS (",
        "  SELECT id FROM credeals.cre_enrichment_queue",
        "  WHERE done_at IS NULL AND attempts < {}".format(MAX_ATTEMPTS),
        "    AND (claimed_at IS NULL OR claimed_at < now() - interval "
        + sql_lit(reclaim_interval) + ")",
        "  ORDER BY priority, enqueued_at",
        "  LIMIT {}".format(batch),
        "  FOR UPDATE SKIP LOCKED",
        ")",
        "UPDATE credeals.cre_enrichment_queue q",
        "   SET claimed_at = now()",
        "  FROM claimed WHERE q.id = claimed.id",
        "RETURNING q.id, q.source_key, q.external_id, q.url, q.reason, q.attempts;",
        "COMMIT;",
    ])


def build_collect_argv(claim_path, out_path):
    """The targeted-detail collect invocation. No full/monitor flags.

    `--enrich-input` puts collect.ts into per-listing detail mode; absent it the
    full and monitor paths are byte-identical (lib/config.ts parseArgs is additive).
    """
    return [
        "npx", "tsx", "collect.ts",
        "--enrich-input", claim_path,
        "--out", out_path,
    ]


def build_ingest_argv(enriched_path):
    """THE safety guard: the ingest call is ALWAYS exactly ["--in", path].

    Never --mark-missing / --no-mark-missing (not a cre_ingest.py flag) /
    --activate-status. Additive is cre_ingest.py's default, so a partial artifact
    is safe (upsert keyed on (brokerage_id, external_id); L1 price COALESCE keeps
    prior prices; the status-flip breaker is inert while status activation is off).
    """
    return ["--in", enriched_path]


def select_done_and_retry(claimed_rows, enriched_listings):
    """Partition the claimed rows by URL presence in the enriched artifact.

    Completion is URL-keyed, NOT external-id-keyed: the queue row carries the
    folded/prefixed external_id (e.g. ``main:usa1``) while the artifact carries the
    native id (``usa1``), but the url is verbatim in both, so the join is exact and
    needs no prefix logic.

    Returns (done_ids, retry_ids, dead_ids):
      - done_ids: claimed row ids whose urls are present in the artifact. The
        URL match decides completion, but deletion is id-keyed so another queued
        reason for the same listing URL is not removed accidentally.
      - retry_ids: claimed-but-absent rows that stay under MAX_ATTEMPTS after the
        +1 increment (re-claimed next run).
      - dead_ids:  claimed-but-absent rows that reach MAX_ATTEMPTS on the +1
        increment (they leave the `attempts < 5` drain set and surface in
        v_cre_enrichment_dead).
    Both retry_ids and dead_ids receive attempts+1 (one increment SQL covers
    both); they differ only in whether the increment crosses the dead threshold.
    """
    enriched_urls = {row.get("url") for row in enriched_listings if row.get("url")}
    done_ids = []
    retry_ids = []
    dead_ids = []
    for r in claimed_rows:
        if r.get("url") in enriched_urls:
            done_ids.append(r["id"])
        elif int(r.get("attempts", 0)) + 1 >= MAX_ATTEMPTS:
            dead_ids.append(r["id"])
        else:
            retry_ids.append(r["id"])
    return done_ids, retry_ids, dead_ids


def build_complete_sql(done_ids):
    """DELETE the completed claimed rows by id, sql_lit-quoted as uuid.

    The queue is an ephemeral work queue (cre_listing_events is the durable
    audit). Deleting done rows is what lets a LATER change to the same listing
    re-enqueue: keeping them would let the monitor's
    `ON CONFLICT (brokerage_id, external_id, reason) DO NOTHING` suppress every
    future change. Completion is still URL-matched in select_done_and_retry(),
    but this DELETE must be id-keyed because the queue can hold separate reasons
    for the same listing URL. Each id goes through sql_lit (quote-doubling) under
    the standard_conforming_strings pin; ids are never f-string'd.

    Empty input -> a no-op transaction (no malformed `IN ()`).
    """
    ids = sorted(done_ids)
    head = [
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
    ]
    if not ids:
        return "\n".join(head + ["COMMIT;"])
    in_list = "(" + ", ".join(sql_lit(i) + "::uuid" for i in ids) + ")"
    return "\n".join(head + [
        "DELETE FROM credeals.cre_enrichment_queue",
        " WHERE id IN " + in_list + ";",
        "COMMIT;",
    ])


def build_release_sql(claimed_ids, last_error=None):
    """Whole-run-failure release: free the claims, attempts UNTOUCHED.

    Used when the collect subprocess fails or the artifact is missing / invalid /
    empty, so the batch retries free on the next run without burning an attempt
    (a systemic outage must not dead-letter healthy rows). last_error is optional;
    ids are uuids emitted as sql_lit(id)::uuid. Empty input -> no-op transaction.
    """
    ids = list(claimed_ids)
    head = [
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
    ]
    if not ids:
        return "\n".join(head + ["COMMIT;"])
    in_list = "(" + ", ".join(sql_lit(i) + "::uuid" for i in ids) + ")"
    return "\n".join(head + [
        "UPDATE credeals.cre_enrichment_queue",
        "   SET claimed_at = NULL,",
        "       last_error = " + _lit_or_null(last_error),
        " WHERE id IN " + in_list + ";",
        "COMMIT;",
    ])


def build_retry_increment_sql(retry_ids, last_error=None):
    """Increment attempts on the claimed-but-absent set AFTER a successful collect.

    Only this set is incremented, and only after collect+ingest succeeded
    (a whole-run failure uses build_release_sql instead, which leaves attempts
    untouched). claimed_at is released so the next run can re-claim; the reclaim
    window (claimed_at < now()-1h) is the backstop either way. When attempts hits
    MAX_ATTEMPTS the row leaves the `attempts < 5` drain set and surfaces in
    v_cre_enrichment_dead. ids are uuids; last_error is optional. Empty input ->
    no-op transaction.
    """
    ids = list(retry_ids)
    head = [
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
    ]
    if not ids:
        return "\n".join(head + ["COMMIT;"])
    in_list = "(" + ", ".join(sql_lit(i) + "::uuid" for i in ids) + ")"
    return "\n".join(head + [
        "UPDATE credeals.cre_enrichment_queue",
        "   SET attempts = attempts + 1,",
        "       claimed_at = NULL,",
        "       last_error = " + _lit_or_null(last_error),
        " WHERE id IN " + in_list + ";",
        "COMMIT;",
    ])


def om_parse_enabled(args):
    """The OM-parse step is OPT-IN and default-OFF. It runs only when --om-parse
    is passed OR CRE_OM_PARSE is a truthy env value. Default enrich behavior is
    byte-identical without it (the existing Tier-B flow is untouched)."""
    if getattr(args, "om_parse", False):
        return True
    return os.environ.get("CRE_OM_PARSE", "").strip().lower() in ("1", "true", "yes", "on")


def build_om_parse_argv(source_keys, *, apply, limit=None):
    """The OM-parse step invocation: shell out to om_parse.py over the enriched
    sources. om_parse re-ingests ADDITIVELY (argv strictly ["--in", path]) so it
    can never soft-delete or activate status. --apply mirrors the enrich run's
    real-vs-dry posture: a dry enrich run keeps the OM step dry too.

    source_keys is a sorted, de-duplicated list (a stable argv for testability).
    """
    argv = [sys.executable, os.path.join(HERE, "om_parse.py"),
            "--sources", ",".join(sorted(set(source_keys)))]
    if limit is not None:
        argv += ["--limit", str(int(limit))]
    if apply:
        argv.append("--apply")
    return argv


def claim_rows_to_items(claimed_rows):
    """Shape claimed rows into the claim.json `items` collect.ts --enrich-input reads.

    Each item carries sourceKey/externalId/url (+ transaction default "sale"; the
    queue does not store transaction and ingest merges sale+lease anyway, so a
    slightly-wrong tag on a partial enrich row cannot blank data).
    """
    items = []
    for r in claimed_rows:
        items.append({
            "sourceKey": r.get("source_key"),
            "externalId": r.get("external_id"),
            "url": r.get("url"),
            "transaction": r.get("transaction") or "sale",
        })
    return {"items": items}


# ---------------------------------------------------------------------------
# psql plumbing (mirrors cre_monitor._psql_read / the apply-mode -f path)
# ---------------------------------------------------------------------------


def _psql_query(db_url, sql):
    """Run one statement-set and return the RETURNING tuples as a list of tuples.

    Uses -tA -F$'\\t' so NULL renders as an empty field (mirrors
    cre_monitor._psql_read). SQL is fed on STDIN via `-f -` (not `-c`): the claim
    script carries a psql meta-command head (`\\set ON_ERROR_STOP on`) plus a
    BEGIN/COMMIT block, and `-c` does not process backslash meta-commands (it
    mis-parses the script). Never prints the DB url.
    """
    psql = find_psql()
    proc = subprocess.run(
        [psql, db_url, "-q", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"psql read failed ({proc.returncode}): {proc.stderr.strip()}")
    rows = []
    for line in proc.stdout.splitlines():
        if line == "":
            continue
        rows.append(tuple(line.split("\t")))
    return rows


def _psql_exec(db_url, sql):
    """Run one transaction script on STDIN via psql -f -. Never prints the DB url.

    The scripts carry a psql meta-command head (`\\set ON_ERROR_STOP on`) plus a
    BEGIN/COMMIT block, so they must be fed on stdin (`-f -`), NOT `-c`: `-c` does
    not process backslash meta-commands and mis-parses the script (the `\\set`
    swallows the rest as its value). `-q` suppresses command-status tags.
    """
    psql = find_psql()
    proc = subprocess.run(
        [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql,
        stdout=sys.stderr, stderr=sys.stderr, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"psql exec failed ({proc.returncode})")


def _parse_claimed(rows):
    """Parse RETURNING tuples (id, source_key, external_id, url, reason, attempts)."""
    claimed = []
    for t in rows:
        # Pad defensively in case a trailing empty field is dropped by splitlines.
        t = tuple(t) + ("",) * (6 - len(t))
        cid, source_key, external_id, url, reason, attempts = t[:6]
        claimed.append({
            "id": cid,
            "source_key": source_key or None,
            "external_id": external_id or None,
            "url": url or None,
            "reason": reason or None,
            "attempts": int(attempts) if attempts not in ("", None) else 0,
        })
    return claimed


def _load_enriched_listings(enriched_path):
    """Read the enriched artifact and return its listings list, or None if the
    artifact is missing / invalid JSON / has zero listings. None signals a
    whole-run failure (release claims, do not ingest)."""
    if not enriched_path or not os.path.isfile(enriched_path):
        return None
    try:
        with open(enriched_path) as f:
            data = json.load(f)
    except (ValueError, OSError):
        return None
    listings = data.get("listings")
    if not isinstance(listings, list) or len(listings) == 0:
        return None
    return listings


# ---------------------------------------------------------------------------
# run() orchestration (thin; design Section 4 flow)
# ---------------------------------------------------------------------------


def run(args):
    batch = int(args.batch)
    if batch < 1:
        sys.exit("--batch must be a positive integer")

    claim_sql = build_claim_sql(batch)

    if args.dry_run:
        # Never connect; print the claim SQL so the shape is auditable. No url.
        print("dry-run: not connecting", file=sys.stderr)
        print(claim_sql)
        return 0

    db_url, env_path = load_db_url(args.env_file)
    print(f"credentials: {env_path}", file=sys.stderr)

    # (1) Claim a batch atomically.
    claimed_rows = _parse_claimed(_psql_query(db_url, claim_sql))
    if not claimed_rows:
        print("0 claimed: queue empty, nothing to enrich", file=sys.stderr)
        return 0
    print(f"claimed {len(claimed_rows)} row(s)", file=sys.stderr)

    # (2) Write the claim artifact for collect.ts --enrich-input.
    os.makedirs(OUT_ENRICH_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    claim_path = os.path.join(OUT_ENRICH_DIR, f"claim_{stamp}.json")
    enriched_path = os.path.join(OUT_ENRICH_DIR, f"enriched_{stamp}.json")
    with open(claim_path, "w") as f:
        json.dump(claim_rows_to_items(claimed_rows), f, indent=2)

    claimed_ids = [r["id"] for r in claimed_rows]

    # (3) Targeted-detail collect. No full/monitor flags.
    collect = subprocess.run(
        build_collect_argv(claim_path, enriched_path),
        cwd=HERE, stdout=sys.stderr, stderr=sys.stderr,
    )

    # (4) GATE on collect success before ingesting. A whole-run failure releases
    # the claims (attempts untouched) so the batch retries free next run.
    enriched_listings = None
    if collect.returncode == 0:
        enriched_listings = _load_enriched_listings(enriched_path)
    if collect.returncode != 0 or enriched_listings is None:
        err = (
            f"collect rc={collect.returncode}"
            if collect.returncode != 0
            else "enriched artifact missing/invalid/empty"
        )
        _psql_exec(db_url, build_release_sql(claimed_ids, last_error=err))
        print(f"enrich run failed: {err}; released {len(claimed_ids)} claim(s)",
              file=sys.stderr)
        return 1

    # (5) Additive ingest. ALWAYS ["--in", path]; never --mark-missing /
    # --no-mark-missing / --activate-status.
    ingest = subprocess.run(
        [sys.executable, os.path.join(HERE, "cre_ingest.py"),
         *build_ingest_argv(enriched_path)]
        + (["--env-file", args.env_file] if args.env_file else []),
        cwd=HERE, stdout=sys.stderr, stderr=sys.stderr,
    )
    if ingest.returncode != 0:
        # Release so the batch re-enriches next run; re-ingest is idempotent
        # (upsert key + COALESCE-keep), so at most one wasted render.
        _psql_exec(db_url, build_release_sql(
            claimed_ids, last_error=f"ingest rc={ingest.returncode}"))
        print(f"ingest failed rc={ingest.returncode}; released "
              f"{len(claimed_ids)} claim(s)", file=sys.stderr)
        return 1

    # (6) Complete: DELETE done rows; increment attempts ONLY on the
    # claimed-but-absent set (after a successful collect+ingest).
    done_ids, retry_ids, dead_ids = select_done_and_retry(
        claimed_rows, enriched_listings)
    _psql_exec(db_url, build_complete_sql(done_ids))
    absent = retry_ids + dead_ids
    if absent:
        _psql_exec(db_url, build_retry_increment_sql(
            absent, last_error="claimed but absent from enriched artifact"))
    print(
        f"enrich complete: {len(done_ids)} done (deleted), "
        f"{len(retry_ids)} retry, {len(dead_ids)} dead-lettered",
        file=sys.stderr,
    )

    # (7) OPT-IN OM-parse step (Phase-2 WS2). OFF unless --om-parse / CRE_OM_PARSE.
    # Additive by construction (om_parse re-ingests with argv ["--in", path], never
    # --activate-status / --mark-missing). A failure here is logged and does NOT
    # fail the enrich run: the detail enrich already committed. Runs over the
    # source keys of the just-completed batch so it parses freshly-captured docs.
    if om_parse_enabled(args):
        om_sources = sorted({r.get("source_key") for r in claimed_rows
                             if r.get("source_key")})
        if om_sources:
            om = subprocess.run(
                build_om_parse_argv(om_sources, apply=True)
                + (["--env-file", args.env_file] if args.env_file else []),
                cwd=HERE, stdout=sys.stderr, stderr=sys.stderr,
            )
            if om.returncode != 0:
                print(f"om-parse step failed rc={om.returncode} (enrich run still "
                      "succeeded; OM facts are additive and retry-safe)",
                      file=sys.stderr)
        else:
            print("om-parse step: no source keys in batch, skipped", file=sys.stderr)

    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--batch", type=int, default=200,
                    help="max rows to claim and enrich per run (default 200)")
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the claim SQL and print it; never connect to a DB")
    ap.add_argument("--om-parse", action="store_true",
                    help="after the detail re-ingest, run the OPT-IN OM-parse step "
                         "(om_parse.py) over the batch's sources; additive, never "
                         "soft-deletes or activates status (default off)")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
