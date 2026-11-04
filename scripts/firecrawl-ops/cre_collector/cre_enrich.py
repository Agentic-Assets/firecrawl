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
  - argparse with --batch (default 200), optional exact --source queue filter,
    --env-file, --dry-run.
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
  - Completion is matched by source and URL (verbatim in queue and artifact),
    NOT by external_id (the queue stores the folded/prefixed id, the artifact
    carries the native source id).
  - The worker writes NO verdict marker; cre_run_tier.sh owns
    out/daily/last_run_enrich.json. The worker communicates only via exit code
    (0 = success or empty-queue no-op; nonzero = collect/ingest failure).

OM extraction retirement:
  GetCREdata is the sole production OM extraction writer. This worker never
  invokes om_parse.py, and it deliberately has no --om-parse flag. A legacy
  CRE_OM_PARSE environment value is ignored, so an old launchd environment
  cannot reactivate a second writer.

Usage:
  python3 cre_enrich.py                      # claim <=200, enrich, ingest, complete
  python3 cre_enrich.py --batch 50
  python3 cre_enrich.py --source marcus-millichap --batch 200
  python3 cre_enrich.py --dry-run            # build claim SQL, print it, do not connect

`--source` is an exact queue `source_key` filter for a manual, targeted drain.
It limits the CLAIM itself, so rows from other sources are never claimed, retried,
or attempt-incremented by that invocation. It is intentionally not forwarded to
collect.ts: the claimed input rows are the source-of-truth for targeted detail
enrichment.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone

import newmark_claim_witness
from cre_newmark_page_guard import parse_output

# Reuse cre_ingest verbatim so env-file discovery, SQL escaping, and psql
# discovery are identical to the production ingest path. The worker imports these
# symbols; it never re-implements credential loading or quote-doubling.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cre_ingest import (  # noqa: E402
    artifact_run_identity,
    assert_expected_database_target,
    find_psql,
    generic_enrichment_substantive,
    lifecycle_transaction_lock_sql,
    load_db_url,
    matthews_targeted_sql_context,
    psql_connection_args,
    psql_connection_env,
    require_canonical_artifact,
    sql_lit,
    validate_hanley_enrichment_artifact,
    validate_matthews_targeted_artifact,
    validate_matthews_targeted_claims,
    validate_strict_artifact_freshness,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ENRICH_DIR = os.path.join(HERE, "out", "enrich")

# Rows whose attempts reach this value fall out of the drain set (attempts < 5)
# and surface in credeals.v_cre_enrichment_dead (sql/010). Mirrors the claim SQL
# `attempts < 5` predicate and select_done_and_retry's dead-letter threshold.
MAX_ATTEMPTS = 5

# Serialize queue replacement and worker claims in Postgres as well as through
# the launchd filesystem lock. This closes the direct-CLI race where a worker
# could claim a row after a source-refresh preflight but before that refresh
# deleted and rebuilt the source's queue.
QUEUE_MUTATION_ADVISORY_LOCK = 1_687_068_469

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


def _normalize_source_filter(source):
    """Return a non-blank exact source key, or None when no filter is requested.

    Queue source keys are stable machine identifiers (for example,
    ``marcus-millichap``), not display names. Whitespace-only input is rejected
    rather than silently widening a manual drain to every source.
    """
    if source is None:
        return None
    source = str(source).strip()
    if not source:
        raise ValueError("--source must be a non-empty queue source_key")
    return source


def build_claim_sql(batch, *, source=None, reclaim_interval=RECLAIM_INTERVAL, targeted=False, source_detail_witness=False, source_page_witness=False):
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
    source = _normalize_source_filter(source)
    if source_detail_witness and (source not in (None, "newmark") or targeted):
        raise ValueError("source detail witness requires mixed or Newmark-only non-targeted claims")
    if source_page_witness and (source != "newmark" or batch != 1 or targeted or source_detail_witness):
        raise ValueError("Newmark page witness requires a Newmark-only one-item claim")
    where_source = []
    if source is not None:
        # This predicate lives inside the locked claim CTE. Consequently a
        # --source run cannot touch unrelated rows, including their attempts.
        where_source.append("    AND source_key = " + sql_lit(source))
    if targeted:
        where_source.append("""    AND EXISTS (
      SELECT 1 FROM credeals.cre_listings l JOIN credeals.cre_source_index si
        ON si.brokerage_id=l.brokerage_id AND si.external_id=l.external_id
      WHERE l.brokerage_id=cre_enrichment_queue.brokerage_id
        AND l.external_id=cre_enrichment_queue.external_id
        AND l.source_url=cre_enrichment_queue.url AND l.status='active' AND l.deleted_at IS NULL
        AND si.source_key='matthews' AND si.url=cre_enrichment_queue.url
        AND si.observation_present IS TRUE AND si.soft_deleted IS FALSE)""")
    return "\n".join([
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "SET LOCAL standard_conforming_strings = on;",
        "SELECT pg_advisory_xact_lock({});".format(
            QUEUE_MUTATION_ADVISORY_LOCK
        ),
        "WITH claimed AS (",
        "  SELECT id FROM credeals.cre_enrichment_queue",
        "  WHERE done_at IS NULL AND attempts < {}".format(MAX_ATTEMPTS),
        "    AND (claimed_at IS NULL OR claimed_at < now() - interval "
        + sql_lit(reclaim_interval) + ")",
        *where_source,
        "  ORDER BY " + ("claimed_at NULLS FIRST, " if source_page_witness else "") + "priority, enqueued_at",
        "  LIMIT {}".format(batch),
        "  FOR UPDATE SKIP LOCKED",
        ")",
        "UPDATE credeals.cre_enrichment_queue q",
        "   SET claimed_at = now()",
        "  FROM claimed WHERE q.id = claimed.id",
        "RETURNING q.id, q.source_key, q.external_id, q.url, q.reason, q.attempts"
        + (", q.claimed_at" if targeted else "")
        + ((", NULL::text, CASE WHEN q.source_key = 'newmark' THEN " + newmark_claim_witness.returning_sql() + " ELSE NULL END") if source_detail_witness else "")
        + ((", q.claimed_at, " + newmark_claim_witness.page_returning_sql() + ", q.brokerage_id") if source_page_witness else "") + ";",
        "COMMIT;",
    ])


def build_collect_argv(claim_path, out_path, *, targeted=False):
    """The targeted-detail collect invocation. No full/monitor flags.

    `--enrich-input` puts collect.ts into per-listing detail mode; absent it the
    full and monitor paths are byte-identical (lib/config.ts parseArgs is additive).
    """
    return [
        "npx", *(["--no-install"] if targeted else []), "tsx", "collect.ts",
        "--enrich-input", claim_path,
        "--out", out_path,
    ] + (["--matthews-targeted-refresh", "--concurrency", "1"] if targeted else [])


def build_ingest_argv(enriched_path, *, targeted_claims=None):
    """THE safety guard: the ingest call is ALWAYS exactly ["--in", path].

    Never --mark-missing / --no-mark-missing (not a cre_ingest.py flag) /
    --activate-status. Additive is cre_ingest.py's default, so a partial artifact
    is safe (upsert keyed on (brokerage_id, external_id); L1 price COALESCE keeps
    prior prices; the status-flip breaker is inert while status activation is off).
    """
    return ["--in", enriched_path] + (
        ["--matthews-targeted-claims", targeted_claims] if targeted_claims else []
    )


def select_done_and_retry(claimed_rows, enriched_listings):
    """Partition the claimed rows by source and URL presence in the enriched artifact.

    Completion is source-and-URL-keyed, NOT external-id-keyed: the queue row carries the
    folded/prefixed external_id (e.g. ``main:usa1``) while the artifact carries the
    native id (``usa1``), but the url is verbatim in both, so the join is exact and
    needs no prefix logic.

    Returns (done_ids, retry_ids, dead_ids):
      - done_ids: claimed row ids whose urls are present in the artifact. The
        source-and-URL match decides completion, but deletion is id-keyed so another queued
        reason for the same listing URL is not removed accidentally.
      - retry_ids: claimed-but-absent rows that stay under MAX_ATTEMPTS after the
        +1 increment (re-claimed next run).
      - dead_ids:  claimed-but-absent rows that reach MAX_ATTEMPTS on the +1
        increment (they leave the `attempts < 5` drain set and surface in
        v_cre_enrichment_dead).
    The caller defers explicit source failures and unattempted claims before
    incrementing the remaining retry and dead-letter candidates.
    """
    enriched_urls = {(row.get("sourceKey"), row.get("url")) for row in enriched_listings if row.get("url")}
    done_ids = []
    retry_ids = []
    dead_ids = []
    for r in claimed_rows:
        if (r.get("source_key"), r.get("url")) in enriched_urls:
            done_ids.append(r["id"])
        elif int(r.get("attempts", 0)) + 1 >= MAX_ATTEMPTS:
            dead_ids.append(r["id"])
        else:
            retry_ids.append(r["id"])
    return done_ids, retry_ids, dead_ids


def newmark_403_claim_ids(claimed_rows, artifact):
    """Identify a bound 403 observation without inferring absent status."""
    if len(claimed_rows) != 1 or claimed_rows[0].get("source_key") != "newmark":
        return set()
    row = claimed_rows[0]
    sources = artifact.get("sources")
    if not isinstance(sources, list):
        return set()
    for source in sources:
        if not isinstance(source, dict) or source.get("sourceKey") != "newmark":
            continue
        observations = source.get("retainedObservations")
        if not isinstance(observations, list):
            continue
        for observation in observations:
            if not isinstance(observation, dict) or observation.get("event") != "generic_enrichment_observation":
                continue
            claim = observation.get("claim")
            doc = observation.get("scrapedDoc")
            metadata = doc.get("metadata") if isinstance(doc, dict) else None
            status = metadata.get("statusCode") if isinstance(metadata, dict) else None
            if (isinstance(claim, dict) and claim.get("sourceKey") == "newmark"
                    and claim.get("externalId") == row.get("external_id")
                    and claim.get("url") == row.get("url")
                    and type(status) is int and status == 403):
                return {row["id"]}
    return set()


def deferred_claim_ids(claimed_rows, artifact):
    """Defer only source failures or claims explicitly marked unattempted/stopped."""
    sources = artifact.get("sources")
    if not isinstance(sources, list):
        return set()
    claimed = {}
    for row in claimed_rows:
        key = (row.get("source_key"), row.get("external_id"), row.get("url"))
        claimed.setdefault(key, []).append(row["id"])
    deferred = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_key = source.get("sourceKey")
        evidence_error = source.get("evidenceError")
        stop_reason = source.get("stopReason")
        if source.get("error") or (source_key == "newmark" and isinstance(stop_reason, str) and stop_reason in {
            "access_denial", "possible_challenge_header", "uncertain_settlement",
        }) or (isinstance(evidence_error, str) and evidence_error.startswith("newmark_detail_")):
            deferred.update(row["id"] for row in claimed_rows if row.get("source_key") == source_key)
            continue
        observations = source.get("retainedObservations")
        if not isinstance(observations, list):
            observations = []
        if source_key == "newmark" and isinstance(evidence_error, str) and evidence_error in {
            "enrichment evidence count exceeds bound", "enrichment evidence bytes exceed bound",
        }:
            retained = set()
            for obs in observations:
                if isinstance(obs, dict) and isinstance(obs.get("claim"), dict):
                    claim = obs["claim"]
                    if (claim.get("sourceKey") == source_key and isinstance(claim.get("externalId"), str)
                            and isinstance(claim.get("url"), str)):
                        retained.add((source_key, claim["externalId"], claim["url"]))
            for row in claimed_rows:
                key = (row.get("source_key"), row.get("external_id"), row.get("url"))
                if row.get("source_key") == source_key and key not in retained:
                    deferred.add(row["id"])
        for obs in observations:
            if not isinstance(obs, dict):
                continue
            failure = obs.get("failure")
            if not obs.get("stopReason") and not (
                isinstance(failure, str) and failure.startswith("not_attempted_after_")
            ):
                continue
            claim = obs.get("claim")
            if (not isinstance(claim, dict) or claim.get("sourceKey") != source_key
                    or not isinstance(claim.get("externalId"), str) or not isinstance(claim.get("url"), str)):
                continue
            key = (source_key, claim.get("externalId"), claim.get("url"))
            deferred.update(claimed.get(key, ()))
    return deferred


def build_complete_sql(done_ids):
    """DELETE the completed claimed rows by id, sql_lit-quoted as uuid.

    The queue is an ephemeral work queue (cre_listing_events is the durable
    audit). Deleting done rows is what lets a LATER change to the same listing
    re-enqueue: keeping them would let the monitor's
    `ON CONFLICT (brokerage_id, external_id, reason) DO NOTHING` suppress every
    future change. Completion is source-and-URL-matched in select_done_and_retry(),
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


def build_release_sql(claimed_ids, last_error=None, *, preserve_claim=False):
    """Release claims without incrementing attempts, or keep their cooldown.

    Used when the collect subprocess fails or the artifact is missing / invalid /
    empty, so the batch retries free on the next run without burning an attempt
    (a systemic outage must not dead-letter healthy rows). last_error is optional;
    ids are uuids emitted as sql_lit(id)::uuid. With preserve_claim, keep
    claimed_at for the existing one-hour reclaim window. Empty input is a no-op.
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
        "   SET " + ("" if preserve_claim else "claimed_at = NULL, ")
        + "last_error = " + _lit_or_null(last_error),
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


def build_newmark_page_disposition_sql(row, outcome, *, last_error=None):
    """Settle only the exact claimed generation; never mutate its successor."""
    if outcome not in {"complete", "retry", "release", "defer"} or not row.get("claimed_at"):
        raise ValueError("invalid Newmark page claim disposition")
    predicate = (
        "id=" + sql_lit(row["id"]) + "::uuid AND source_key='newmark'"
        + " AND brokerage_id IS NOT DISTINCT FROM " + _lit_or_null(row.get("brokerage_id")) + "::uuid"
        + " AND external_id IS NOT DISTINCT FROM " + _lit_or_null(row.get("external_id"))
        + " AND url IS NOT DISTINCT FROM " + _lit_or_null(row.get("url"))
        + " AND claimed_at=" + sql_lit(row["claimed_at"]) + "::timestamptz"
        + " AND attempts=" + str(int(row["attempts"])) + " AND done_at IS NULL"
    )
    if outcome == "complete":
        statement = "DELETE FROM credeals.cre_enrichment_queue WHERE " + predicate
    else:
        updates = []
        if outcome == "retry":
            updates.append("attempts=attempts+1")
        if outcome != "defer":
            updates.append("claimed_at=NULL")
        updates.append("last_error=" + _lit_or_null(last_error))
        statement = "UPDATE credeals.cre_enrichment_queue SET " + ", ".join(updates) + " WHERE " + predicate
    return "\\set ON_ERROR_STOP on\nBEGIN;\n" + lifecycle_transaction_lock_sql() + (
        "\nWITH changed AS (" + statement + " RETURNING id) SELECT count(*) FROM changed;\nCOMMIT;\n")


def settle_newmark_page_claim(db_url, row, outcome, *, last_error=None):
    result = _psql_query(db_url, build_newmark_page_disposition_sql(row, outcome, last_error=last_error))
    if result not in [[("0",)], [("1",)]]:
        raise ValueError("unknown Newmark page queue disposition")
    return result == [("1",)]


def claim_rows_to_items(claimed_rows, *, targeted=False):
    """Shape claimed rows into the claim.json `items` collect.ts --enrich-input reads.

    Each item carries sourceKey/externalId/url. The queue does not store tenure;
    SVN and Newmark leave it unknown. Other sources retain the sale fallback.
    """
    items = []
    for r in claimed_rows:
        item = {
            "sourceKey": r.get("source_key"),
            "externalId": r.get("external_id"),
            "url": r.get("url"),
        }
        if r.get("source_key") in {"svn", "newmark"} and r.get("transaction") is None:
            pass  # These queues do not witness a transaction type.
        elif not targeted or r.get("source_key") != "matthews" or r.get("transaction") is not None:
            item["transaction"] = r.get("transaction") or "sale"
        if r.get("claimed_at"):
            item.update(claimId=r["id"], claimedAt=r["claimed_at"])
        if r.get("source_key") == "newmark" and r.get("source_published_detail") is not None:
            item["sourcePublishedDetail"] = r["source_published_detail"]
        if (r.get("source_key") == "newmark" and r.get("source_page_witness") is not None
                and r["source_page_witness"] != {"refused": True}):
            item["sourcePageWitness"] = r["source_page_witness"]
        if "source_page_witness" in r:
            item["sourcePageClaim"] = {"id": r["id"], "claimedAt": r.get("claimed_at"),
                "attempts": r["attempts"], "brokerageId": r.get("brokerage_id")}
        items.append(item)
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
        [
            psql,
            *psql_connection_args(db_url),
            "-q",
            "-tA",
            "-F",
            "\t",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        env=psql_connection_env(db_url),
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
        [
            psql,
            *psql_connection_args(db_url),
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        env=psql_connection_env(db_url),
        input=sql,
        stdout=sys.stderr, stderr=sys.stderr, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"psql exec failed ({proc.returncode})")


def _parse_claimed(rows, *, source_detail_witness=False, source_page_witness=False):
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
            **({"claimed_at": t[6]} if len(t) > 6 and t[6] else {}),
            **({"source_published_detail": newmark_claim_witness.parse(t[7])}
               if source_detail_witness and len(t) > 7 else {}),
            **({"source_page_witness": newmark_claim_witness.parse_page(t[7])}
               if source_page_witness and len(t) > 7 else {}),
            **({"brokerage_id": t[8] or None} if source_page_witness and len(t) > 8 else {}),
        })
    return claimed


def _load_enriched_artifact(enriched_path):
    """Read an enriched artifact, or return None when it is unavailable."""
    if not enriched_path or not os.path.isfile(enriched_path):
        return None
    try:
        with open(enriched_path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def validate_enriched_artifact(claimed_rows, artifact, *, targeted_claims=None):
    """Fail closed unless every emitted row belongs to its claimed source."""
    if not isinstance(artifact, dict) or (artifact.get("runMeta") or {}).get("mode") != "enrich":
        raise ValueError("enriched artifact must declare runMeta.mode == 'enrich'")
    listings = artifact.get("listings")
    if not isinstance(listings, list):
        raise ValueError("enriched artifact must contain a listings array")
    if listings or "terminalCaptureFailure" in artifact["runMeta"]:
        require_canonical_artifact(artifact)
    validate_hanley_enrichment_artifact(artifact)
    claimed_by_url = {}
    for row in claimed_rows:
        key = (row.get("source_key"), row.get("url"))
        claimed_by_url.setdefault(key, []).append(row)
    safe = []
    seen_urls = set()
    for listing in listings:
        url = listing.get("url") if isinstance(listing, dict) else None
        matches = claimed_by_url.get((listing.get("sourceKey"), url), []) if isinstance(listing, dict) else []
        if len({row.get("external_id") for row in matches}) > 1:
            raise ValueError("ambiguous claimed source URL identity")
        claimed = matches[0] if matches else None
        if claimed is None:
            raise ValueError(f"enriched artifact URL was not claimed: {url!r}")
        if listing.get("sourceKey") != claimed.get("source_key"):
            raise ValueError(f"enriched artifact source key mismatch: {url!r}")
        if listing.get("sourceKey") == "hanley" and listing.get("id") != claimed.get(
            "external_id"
        ):
            raise ValueError("Hanley enrichment native identity differs from its claim")
        if "source_page_witness" in claimed and listing.get("id") != claimed.get("external_id"):
            raise ValueError("Newmark enrichment identity differs from its claim")
        if "newmarkPageObservation" in listing and (
            listing.get("sourceKey") != "newmark" or listing.get("id") != claimed.get("external_id")
            or not isinstance(listing["newmarkPageObservation"], dict)
            or listing["newmarkPageObservation"].get("witness") != claimed.get("source_page_witness")
        ):
            raise ValueError("Newmark page observation does not match its claim witness")
        identity = (listing.get("sourceKey"), url)
        if identity in seen_urls:
            raise ValueError(f"enriched artifact contains duplicate URL: {url!r}")
        seen_urls.add(identity)
        if "genericEnrich" in listing and not generic_enrichment_substantive(listing):
            continue
        safe.append(listing)
    if targeted_claims is not None:
        validate_matthews_targeted_artifact(artifact, targeted_claims)
    elif any(listing.get("sourceKey") == "matthews" for listing in safe):
        validate_strict_artifact_freshness(artifact)
    return safe


# ---------------------------------------------------------------------------
# run() orchestration (thin; design Section 4 flow)
# ---------------------------------------------------------------------------


def build_targeted_queue_sql(
    items, operation, *, last_error=None, artifact=None, claims=None
):
    """Compare exact claim tenure before changing any Matthews claim."""
    if operation not in {"complete", "release", "retry", "defer"}:
        raise ValueError("invalid targeted queue operation")
    if not items:
        return "BEGIN; COMMIT;"
    values = ",\n".join(
        "("
        + ",".join(
            sql_lit(item[key]) for key in ("claimId", "externalId", "url", "claimedAt")
        )
        + ")"
        for item in items
    )
    mutation = {
        "complete": "DELETE FROM credeals.cre_enrichment_queue q USING _targeted_claims e WHERE q.id=e.id;",
        "release": "UPDATE credeals.cre_enrichment_queue q SET claimed_at=NULL, last_error="
        + _lit_or_null(last_error)
        + " FROM _targeted_claims e WHERE q.id=e.id;",
        "defer": "UPDATE credeals.cre_enrichment_queue q SET last_error="
        + _lit_or_null(last_error)
        + " FROM _targeted_claims e WHERE q.id=e.id;",
        "retry": "UPDATE credeals.cre_enrichment_queue q SET attempts=attempts+1, claimed_at=NULL, last_error="
        + _lit_or_null(last_error)
        + " FROM _targeted_claims e WHERE q.id=e.id;",
    }[operation]
    queue_lock = """CREATE TEMP TABLE _targeted_locked ON COMMIT DROP AS
SELECT q.id FROM credeals.cre_enrichment_queue q JOIN _targeted_claims e ON q.id=e.id ORDER BY q.id FOR UPDATE OF q;
"""
    witness = f"""DO $$ BEGIN
 IF (SELECT count(*) FROM _targeted_claims) != (SELECT count(*) FROM credeals.cre_enrichment_queue q JOIN _targeted_claims e ON q.id=e.id
   WHERE q.source_key='matthews' AND q.external_id=e.external_id AND q.url=e.url
   AND q.claimed_at IS NOT DISTINCT FROM e.claimed_at AND q.done_at IS NULL
   AND q.attempts < {MAX_ATTEMPTS} AND q.claimed_at > now()-interval '1 hour') THEN
   RAISE EXCEPTION 'targeted Matthews queue tenure changed';
 END IF;
END $$;"""
    completion_guard = ""
    if operation == "complete":
        if artifact is None or claims is None:
            raise ValueError("targeted completion requires exact persisted proof")
        proof = build_targeted_readback_sql(artifact, claims, transaction=False)
        completion_guard = f"""
CREATE TEMP TABLE _completion_source_locks ON COMMIT DROP AS
SELECT si.id FROM credeals.cre_source_index si JOIN credeals.cre_enrichment_queue q
 ON q.brokerage_id=si.brokerage_id AND q.external_id=si.external_id
JOIN _targeted_claims e ON e.id=q.id ORDER BY si.id FOR UPDATE OF si;
CREATE TEMP TABLE _completion_listing_locks ON COMMIT DROP AS
SELECT l.id FROM credeals.cre_listings l JOIN credeals.cre_enrichment_queue q
 ON q.brokerage_id=l.brokerage_id AND q.external_id=l.external_id
JOIN _targeted_claims e ON e.id=q.id ORDER BY l.id FOR UPDATE OF l;
{queue_lock}
{witness}
CREATE TEMP TABLE _completion_readback ON COMMIT DROP AS {proof}
DO $$ BEGIN
 IF (SELECT matching_rows FROM _completion_readback) != {len(items)} THEN
   RAISE EXCEPTION 'targeted Matthews completion readback changed';
 END IF;
END $$;
"""
    return f"""\\set ON_ERROR_STOP on
BEGIN;
SET LOCAL standard_conforming_strings=on;
SELECT pg_advisory_xact_lock({QUEUE_MUTATION_ADVISORY_LOCK});
{lifecycle_transaction_lock_sql() if operation == "complete" else ""}
CREATE TEMP TABLE _targeted_claims ON COMMIT DROP AS
SELECT id::uuid, external_id, url, claimed_at::timestamptz FROM (VALUES {values}) AS v(id,external_id,url,claimed_at);
{completion_guard if operation == "complete" else queue_lock + witness}
{mutation}
COMMIT;
"""


def build_targeted_readback_sql(artifact, claims, *, transaction=True):
    from cre_validate import _freshness_payload_sql

    targets = matthews_targeted_sql_context(artifact, claims)
    values = ",\n".join(
        "("
        + ",".join(
            sql_lit(t[key])
            for key in (
                "externalId",
                "url",
                "canonicalUrl",
                "transactionMode",
                "generationId",
                "inventoryObservedAt",
                "detailObservedAt",
            )
        )
        + ")"
        for t in targets
    )
    sql = f"""WITH expected(external_id,url,canonical_url,tenure,generation,inventory_at,detail_at) AS (VALUES {values}),
observed AS (
 SELECT e.*, l.id, l.source_url, l.canonical_url AS actual_canonical, l.transaction_type,
 l.status, l.deleted_at, l.scraped_at, si.source_key AS index_source,si.url AS index_url,
 si.observation_present, si.soft_deleted, si.last_seen, si.last_enumerated_at,
 {_freshness_payload_sql("l")} AS payload
 FROM expected e LEFT JOIN credeals.cre_brokerages b ON b.slug='matthews'
 LEFT JOIN credeals.cre_listings l ON l.brokerage_id=b.id AND l.external_id=e.external_id
 LEFT JOIN credeals.cre_source_index si ON si.brokerage_id=b.id AND si.external_id=e.external_id)
SELECT CASE WHEN count(*)={len(targets)} AND count(DISTINCT external_id)={len(targets)}
 THEN count(*) ELSE 0 END AS matching_rows FROM observed WHERE id IS NOT NULL AND source_url=url AND actual_canonical=canonical_url
 AND transaction_type IN (tenure,'sale_or_lease') AND status='active' AND deleted_at IS NULL
 AND scraped_at=detail_at::timestamptz
 AND observation_present IS TRUE AND soft_deleted IS FALSE
 AND index_source='matthews' AND index_url=url
 AND last_seen=inventory_at::timestamptz AND last_enumerated_at=inventory_at::timestamptz
 AND payload->'freshnessProvenance'->>'generationId'=generation
 AND (payload->>'inventoryObservedAt')::timestamptz=inventory_at::timestamptz
 AND (payload->>'detailObservedAt')::timestamptz=detail_at::timestamptz
 AND payload->'freshnessProvenance'->>'detailScope'='detail_page'
 AND payload->'freshnessProvenance'->>'cacheDisposition'='live'
 AND payload->>'sourceKey'='matthews' AND payload->>'canonicalUrl'=canonical_url
 AND payload->'freshnessProvenance'->>'method'='matthews_canonical_detail'
 AND payload->'freshnessProvenance'->>'identityMethod'='canonical_property_url'
 AND payload->>'transactionMode'=tenure
 AND payload->>'transactionType'=CASE WHEN tenure='sale' THEN 'Sale' ELSE 'Lease' END;
"""
    if transaction:
        return "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSET LOCAL statement_timeout='30s'; SET LOCAL lock_timeout='2s';\n" + sql + "ROLLBACK;\n"
    return sql


def run(args):
    batch = int(args.batch)
    if batch < 1:
        sys.exit("--batch must be a positive integer")

    try:
        source = _normalize_source_filter(getattr(args, "source", None))
    except ValueError as exc:
        sys.exit(str(exc))
    targeted = getattr(args, "matthews_targeted_refresh", False)
    if targeted and (source != "matthews" or not 1 <= batch <= 50
                     or not getattr(args, "expected_db_target_sha256", None)
                     or not 1 <= getattr(args, "work_seconds", 600) <= 600):
        raise ValueError("targeted Matthews worker requires exact bounded target admission")
    evidence_witness = (os.environ.get("CRE_NEWMARK_PUBLISHED_DETAIL") == "1"
                        and getattr(args, "artifact_out", None) is not None and not targeted
                        and source in (None, "newmark"))
    page_witness = (source == "newmark" and batch == 1 and not targeted
                    and not evidence_witness and getattr(args, "artifact_out", None) is not None
                    and getattr(args, "expected_db_target_sha256", None) is not None)
    claim_sql = build_claim_sql(batch, source=source, targeted=targeted, source_detail_witness=evidence_witness,
                                source_page_witness=page_witness)

    if args.dry_run:
        # Never connect; print the claim SQL so the shape is auditable. No url.
        print("dry-run: not connecting", file=sys.stderr)
        print(claim_sql)
        return 0

    # Check explicit output custody before claiming any queue rows. The
    # guarded parent owns this directory and records the matching marker.
    artifact_out = getattr(args, "artifact_out", None)
    claim_path = enriched_path = None
    if artifact_out is not None:
        from pathlib import Path
        from source_evidence.enrichment_custody import preflight
        output = Path(artifact_out)
        if not output.is_absolute() or output.name != "enriched.json":
            raise ValueError("artifact output must be an absolute private enriched.json path")
        claim_file, output_file = preflight(output.parent)
        claim_path, enriched_path = str(claim_file), str(output_file)
    elif not targeted:
        from source_evidence.enrichment_custody import preflight
        os.makedirs(OUT_ENRICH_DIR, exist_ok=True)
        private_root = tempfile.mkdtemp(prefix="enrich-", dir=OUT_ENRICH_DIR)
        claim_file, output_file = preflight(private_root)
        claim_path, enriched_path = str(claim_file), str(output_file)

    def require_owner():
        if targeted:
            from cre_stop_guarded_tier import require_guarded_matthews_parent
            return require_guarded_matthews_parent(
                getattr(args, "guarded_run_id", None), args.expected_db_target_sha256, batch)

    require_owner()

    db_url, env_path = load_db_url(args.env_file)
    expected_target = getattr(args, "expected_db_target_sha256", None)
    assert_expected_database_target(db_url, expected_target)
    print(f"credentials: {env_path}", file=sys.stderr)

    # (1) Claim a batch atomically.
    claimed_rows = _parse_claimed(_psql_query(db_url, claim_sql), source_detail_witness=evidence_witness, source_page_witness=page_witness)
    if not claimed_rows:
        print("0 claimed: queue empty, nothing to enrich", file=sys.stderr)
        return 0
    print(f"claimed {len(claimed_rows)} row(s)", file=sys.stderr)

    # (2) Write the claim artifact for collect.ts --enrich-input.
    if targeted and artifact_out is None:
        from cre_stop_guarded_tier import OUT_ROOT
        unique = uuid.uuid4().hex
        private_root = OUT_ROOT / args.guarded_run_id
        claim_path = str(private_root / ("claim-" + unique + ".json"))
        enriched_path = str(private_root / ("enriched-" + unique + ".json"))
    claims = claim_rows_to_items(claimed_rows, targeted=targeted)
    if targeted:
        claims["matthewsTargetedRefresh"] = {
            "sourceKey": "matthews", "generationId": "matthews-targeted-" + uuid.uuid4().hex,
            "generationStartedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        from cre_stop_guarded_tier import _process_identity
        claims["matthewsGuardedWorker"] = {
            "runId": args.guarded_run_id, "workerIdentity": _process_identity(os.getpid()),
        }
    descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as f:
        json.dump(claims, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    claim_dir_fd = os.open(os.path.dirname(claim_path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(claim_dir_fd)
    finally:
        os.close(claim_dir_fd)
    if targeted:
        validate_matthews_targeted_claims(claims)

    claimed_ids = [r["id"] for r in claimed_rows]
    ambiguous_page = page_witness and claimed_rows[0].get("source_page_witness") == {"refused": True}

    # (3) Targeted-detail collect. No full/monitor flags.
    collect_env = dict(os.environ)
    if targeted:
        collect_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "FIRECRAWL_API_URL", "API_URL",
            "CRE_PERFORMANCE_PATH", "CRE_PERFORMANCE_COMMAND_ID",
        }}
        collect_env.update(CRE_REFRESH_GENERATION=claims["matthewsTargetedRefresh"]["generationId"],
                           CRE_REFRESH_STARTED_AT=claims["matthewsTargetedRefresh"]["generationStartedAt"],
                           CRE_REQUIRE_FRESH_DETAILS="1")
    require_owner()
    collect = subprocess.run(
        build_collect_argv(claim_path, enriched_path, targeted=targeted),
        cwd=HERE, stdout=sys.stderr, stderr=sys.stderr,
        **({"env": collect_env} if targeted else {}),
    )

    if ambiguous_page:
        # The retained private artifact is unverified source evidence only.
        # Cooldown leaves attempts intact and lets other queue items progress.
        require_owner()
        changed = settle_newmark_page_claim(db_url, claimed_rows[0], "defer",
                                           last_error="Newmark page identity changed or ambiguous")
        print("Newmark URL association ambiguous; "
              + ("claim deferred; collection evidence retained when available" if changed
                 else "claim superseded; no queue change"), file=sys.stderr)
        return 21

    # (4) GATE on collect success before ingesting. A whole-run failure releases
    # the claims (attempts untouched) so the batch retries free next run.
    enriched_listings = None
    artifact_error = None
    if collect.returncode == 0:
        artifact = _load_enriched_artifact(enriched_path)
        if artifact is not None:
            try:
                enriched_listings = validate_enriched_artifact(
                    claimed_rows, artifact, targeted_claims=claims if targeted else None)
            except ValueError as exc:
                artifact_error = str(exc)
    if collect.returncode != 0 or enriched_listings is None:
        err = (
            f"collect rc={collect.returncode}"
            if collect.returncode != 0
            else artifact_error or "enriched artifact missing/invalid/empty"
        )
        require_owner()
        if page_witness:
            changed = settle_newmark_page_claim(db_url, claimed_rows[0], "release", last_error=err)
            print("Newmark collection failed; " + ("claim released" if changed else "claim superseded; no queue change"), file=sys.stderr)
            return 21
        _psql_exec(db_url, build_targeted_queue_sql(claims["items"], "release", last_error=err)
                   if targeted else build_release_sql(claimed_ids, last_error=err))
        print(f"enrich run failed: {err}; released {len(claimed_ids)} claim(s)",
              file=sys.stderr)
        return 1

    if page_witness and any(
        isinstance(observation, dict) and observation.get("newmarkIdentityConflict") is True
        for source_entry in artifact.get("sources", []) if isinstance(source_entry, dict)
        for observation in (source_entry.get("retainedObservations")
                            if isinstance(source_entry.get("retainedObservations"), list) else [])
    ):
        require_owner()
        changed = settle_newmark_page_claim(db_url, claimed_rows[0], "defer",
                                           last_error="Newmark page URL identity contradicted")
        print("Newmark page identity contradicted; "
              + ("claim deferred; private evidence retained" if changed
                 else "claim superseded; no queue change"), file=sys.stderr)
        return 21

    # (5) Additive ingest. ALWAYS ["--in", path]; never --mark-missing /
    # --no-mark-missing / --activate-status. A valid empty artifact means every
    # claimed detail was rejected by admission; skip ingest but continue into
    # retry/dead-letter accounting so that batch cannot loop forever.
    if enriched_listings:
        # Canonical ingest applies the same generic qualification to the
        # original artifact. Retaining that private file is not evidence-store
        # database admission or proof of a successful write.
        require_owner()
        page_rows = [row for row in enriched_listings if "newmarkPageObservation" in row]
        page_attempt = None
        if page_rows or page_witness:
            if not page_witness or len(claimed_rows) != 1 or len(enriched_listings) != 1:
                print("Newmark page requires one bound Newmark-only claim", file=sys.stderr)
                return 1
            code_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True, text=True, check=True).stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
                raise ValueError("invalid Newmark page code binding")
            def stable_digest(path):
                with open(path, "rb") as stream:
                    return hashlib.sha256(stream.read()).hexdigest()
            artifact_digest = stable_digest(enriched_path)
            claim_digest = stable_digest(claim_path)
            page_nonce = uuid.uuid4().hex
            page_attempt = hashlib.sha256(
                (page_nonce + artifact_digest + claim_digest + expected_target + code_sha).encode()
            ).hexdigest()
        ingest = subprocess.run(
            [sys.executable, os.path.join(HERE, "cre_ingest.py"),
             *build_ingest_argv(enriched_path, targeted_claims=claim_path if targeted else None)]
            + (["--env-file", args.env_file] if args.env_file else [])
            + (["--expected-db-target-sha256", expected_target]
               if expected_target is not None else [])
            + (["--newmark-page-attempt", page_attempt, "--newmark-page-claims", claim_path,
                "--skip-post-commit-summary"] if page_attempt else []),
            cwd=HERE, stdout=subprocess.PIPE if page_attempt else sys.stderr, stderr=sys.stderr,
            text=bool(page_attempt), check=False,
        )
        if ingest.returncode != 0:
            if expected_target is not None:
                # A guarded owner must reconcile uncertain ingest before any
                # claim can be retried. A nonzero client exit is not rollback
                # proof; leave claims and its durable recovery fence intact.
                print("bound ingest did not complete; claims retained for operator recovery",
                      file=sys.stderr)
                return 1
            # Release so the batch re-enriches next run; re-ingest is idempotent
            # (upsert key + COALESCE-keep), so at most one wasted render.
            _psql_exec(db_url, build_release_sql(
                claimed_ids, last_error=f"ingest rc={ingest.returncode}"))
            print(f"ingest failed rc={ingest.returncode}; released "
                  f"{len(claimed_ids)} claim(s)", file=sys.stderr)
            return 1
        if page_attempt:
            if stable_digest(enriched_path) != artifact_digest or stable_digest(claim_path) != claim_digest:
                print("Newmark page artifact changed; claim retained for recovery", file=sys.stderr)
                return 1
            try:
                outcome = parse_output(ingest.stdout, page_attempt)
            except ValueError:
                print("Newmark page outcome unknown; claim retained for recovery", file=sys.stderr)
                return 1
            if outcome == "committed":
                print("Newmark page commit proof:", page_attempt, "nonce", page_nonce,
                      "artifact", artifact_digest, "claim", claim_digest, "target", expected_target,
                      "code", code_sha, file=sys.stderr)
            if outcome == "refused":
                run_key, _ = artifact_run_identity([enriched_path])
                count = _psql_query(db_url, "SELECT count(*) FROM credeals.cre_scrape_jobs WHERE artifact_run_key=" + sql_lit(run_key) + ";")
                if count != [("0",)]:
                    print("Newmark page rollback not confirmed; claim retained", file=sys.stderr)
                    return 1
                print("Newmark page rollback proof:", page_attempt, "nonce", page_nonce,
                      "artifact", artifact_digest, "claim", claim_digest, "target", expected_target,
                      "code", code_sha, "zero_jobs", file=sys.stderr)
                require_owner()
                changed = settle_newmark_page_claim(db_url, claimed_rows[0], "defer",
                    last_error="Newmark page identity changed or ambiguous")
                print("Newmark identity refusal confirmed; " + ("claim deferred" if changed else "claim superseded; no queue change"), file=sys.stderr)
                return 21
    else:
        print(
            "0 admitted listings: skipping ingest and classifying queue outcomes",
            file=sys.stderr,
        )

    # (6) Complete: DELETE done rows; increment attempts ONLY on the
    # claimed-but-absent set (after a successful collect+ingest).
    done_ids, retry_ids, dead_ids = select_done_and_retry(
        claimed_rows, enriched_listings)
    if targeted and enriched_listings:
        require_owner()
        readback = _psql_query(db_url, build_targeted_readback_sql(artifact, claims))
        if readback != [(str(len(enriched_listings)),)]:
            print("targeted readback refused; claims retained for recovery", file=sys.stderr)
            return 1
    require_owner()
    if page_witness and done_ids:
        if not settle_newmark_page_claim(db_url, claimed_rows[0], "complete"):
            print("Newmark committed claim was superseded; recovery required", file=sys.stderr)
            return 1
    else:
        _psql_exec(db_url, build_targeted_queue_sql(
            [i for i in claims["items"] if i["claimId"] in done_ids], "complete",
            artifact=artifact, claims=claims) if targeted else build_complete_sql(done_ids))
    denied = newmark_403_claim_ids(claimed_rows, artifact) if page_witness else set()
    deferred = deferred_claim_ids(claimed_rows, artifact) | denied
    absent = [claim_id for claim_id in retry_ids + dead_ids if claim_id not in deferred]
    deferred_ids = [claim_id for claim_id in retry_ids + dead_ids if claim_id in deferred]
    if deferred_ids:
        if page_witness:
            if not settle_newmark_page_claim(db_url, claimed_rows[0], "defer",
                                             last_error="Newmark provider HTTP 403" if denied else "enrichment attempt not established"):
                print("Newmark claim superseded; no queue change", file=sys.stderr)
                return 21
        else:
            _psql_exec(db_url, build_targeted_queue_sql(
                [i for i in claims["items"] if i["claimId"] in deferred_ids], "defer",
                last_error="enrichment attempt not established") if targeted else build_release_sql(
                deferred_ids, last_error="enrichment attempt not established", preserve_claim=True))
    if absent:
        if page_witness:
            if not settle_newmark_page_claim(db_url, claimed_rows[0], "retry", last_error="claimed but absent from enriched artifact"):
                print("Newmark claim superseded; no queue change", file=sys.stderr)
                return 21
        else:
            _psql_exec(db_url, build_targeted_queue_sql(
                [i for i in claims["items"] if i["claimId"] in absent], "retry",
                last_error="claimed but absent from enriched artifact") if targeted else build_retry_increment_sql(
                absent, last_error="claimed but absent from enriched artifact"))
    state = "completed" if not absent and not deferred_ids else ("partial" if done_ids else "failed")
    code = {"completed": 0, "partial": 20, "failed": 21}[state]
    print(f"enrich {state}: {len(done_ids)} done, {len(absent)} retry, "
          f"{len(deferred_ids)} deferred without established attempt", file=sys.stderr)
    return code


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--batch", type=int, default=200,
                    help="max rows to claim and enrich per run (default 200)")
    ap.add_argument("--matthews-targeted-refresh", action="store_true")
    ap.add_argument("--work-seconds", type=int, default=600)
    ap.add_argument("--guarded-run-id", help=argparse.SUPPRESS)
    ap.add_argument("--artifact-out", help=argparse.SUPPRESS)
    ap.add_argument(
        "--source", default=None, metavar="SOURCE_KEY",
        help=("exact queue source_key to drain; isolates claims and retries to "
              "that source (manual use only)"),
    )
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument(
        "--expected-db-target-sha256", default=None,
        help="require this database target before claims and in the ingest child",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="build the claim SQL and print it; never connect to a DB")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
