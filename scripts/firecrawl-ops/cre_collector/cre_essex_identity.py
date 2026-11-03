"""Exact Essex historical-identity predicate and read-only pre-seal probe."""

import hashlib
import json
import os
import re
from pathlib import Path


def incoming_claims_sql():
    return """SELECT s.brokerage_id, s.external_id, s.source_url,
           ARRAY(SELECT DISTINCT CASE
                   WHEN jsonb_typeof(n.value) = 'object'
                    AND n.value->>'type' = 'wordpress_properties_post'
                    AND n.value->>'canonicalUrl' = s.source_url
                    AND n.value - ARRAY['type','id','canonicalUrl']::text[] = '{}'::jsonb
                   THEN n.value->>'id'
                   ELSE '__invalid_wordpress_page_claim__' END
                 FROM jsonb_path_query(s.raw_data, '$.**.essexWordpressPage') n(value)) AS native_ids
    FROM _src s
    WHERE jsonb_path_exists(s.raw_data, '$.**.sourceKey ? (@ == "essex-realty")')"""


def invalid_incoming_sql():
    return """SELECT s.brokerage_id FROM _essex_identity_claims s
    WHERE s.source_url IS NULL OR s.external_id IS NULL
       OR s.source_url !~ '^https://essexrealtygroup[.]com/properties/[^/?#]+/$'
       OR s.external_id IS DISTINCT FROM substr(
           s.source_url, length('https://essexrealtygroup.com/') + 1,
           length(s.source_url) - length('https://essexrealtygroup.com/') - 1)
       OR cardinality(s.native_ids) <> 1
       OR CASE WHEN s.native_ids[1] ~ '^[1-9][0-9]{0,15}$'
               THEN s.native_ids[1]::numeric > 9007199254740991
               ELSE true END"""


def historical_claims_sql():
    return """SELECT t.brokerage_id, t.external_id, t.source_url,
           ARRAY(SELECT DISTINCT CASE
                   WHEN jsonb_typeof(n.value) = 'object'
                    AND n.value->>'type' = 'wordpress_properties_post'
                    AND n.value->>'canonicalUrl' = t.source_url
                    AND n.value - ARRAY['type','id','canonicalUrl']::text[] = '{}'::jsonb
                   THEN n.value->>'id'
                   ELSE '__invalid_wordpress_page_claim__' END
                 FROM jsonb_path_query(t.raw_data, '$.**.essexWordpressPage') n(value)
                 WHERE n.value <> 'null'::jsonb) AS native_ids
    FROM credeals.cre_listings t
    WHERE EXISTS (
      SELECT 1 FROM _essex_identity_claims s WHERE s.brokerage_id = t.brokerage_id
    )"""


def historical_conflicts_sql():
    return """SELECT s.brokerage_id
    FROM _essex_identity_claims s
    JOIN _essex_historical_identity_claims t ON t.brokerage_id = s.brokerage_id
    WHERE (t.source_url = s.source_url AND t.external_id IS DISTINCT FROM s.external_id)
       OR (t.external_id = s.external_id AND t.source_url IS DISTINCT FROM s.source_url)
       OR (s.native_ids[1] = ANY(t.native_ids)
           AND (t.source_url IS DISTINCT FROM s.source_url OR t.external_id IS DISTINCT FROM s.external_id))
       OR ((t.source_url = s.source_url OR t.external_id = s.external_id)
           AND cardinality(t.native_ids) > 0
           AND (cardinality(t.native_ids) <> 1 OR t.native_ids[1] IS DISTINCT FROM s.native_ids[1]))"""


def stage_essex_identity(artifact):
    """Project the same identity-bearing rows as maintained ingest staging."""
    from cre_ingest import to_row

    if not isinstance(artifact, dict) or not isinstance(artifact.get("listings"), list) or not artifact["listings"]:
        raise ValueError("Essex artifact listings missing")
    brokers = {i: row for i, row in enumerate(artifact.get("brokers") or [])}
    finished = (artifact.get("runMeta") or {}).get("finishedAt")
    if not isinstance(finished, str) or not finished:
        raise ValueError("Essex artifact finish time missing")
    result, keys = [], set()
    for listing in artifact["listings"]:
        if not isinstance(listing, dict) or listing.get("sourceKey") != "essex-realty":
            raise ValueError("Essex artifact source differs")
        row = to_row(listing, brokers, finished)
        if not isinstance(row, dict) or row.get("slug") != "essex-realty":
            raise ValueError("Essex staging row missing")
        key = (row.get("slug"), row.get("external_id"))
        if key in keys:
            raise ValueError("duplicate Essex staging identity")
        keys.add(key)
        result.append({name: row[name] for name in ("slug", "external_id", "source_url", "raw_data")})
    return result


def classify_probe_result(rows, expected):
    fields = {"brokerages", "staged", "joined", "claims", "invalid", "conflicts"}
    if (type(expected) is not int or expected <= 0 or not isinstance(rows, list) or len(rows) != 1
            or not isinstance(rows[0], dict) or set(rows[0]) != fields):
        raise ValueError("Essex probe result shape differs")
    values = {}
    for name, raw in rows[0].items():
        if not isinstance(raw, str) or not raw.isdecimal():
            raise ValueError("Essex probe count malformed")
        values[name] = int(raw)
    if (values["brokerages"] != 1 or any(values[name] != expected for name in ("staged", "joined", "claims"))
            or values["invalid"] != 0):
        raise ValueError("Essex probe scope differs")
    return values["conflicts"]


def projection_digest(rows):
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def probe_sql(rows):
    """Use the transaction guard's exact predicates in a read-only query."""
    from cre_ingest import sql_lit

    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"""SET LOCAL statement_timeout = '30s';
WITH _stage AS (
  SELECT * FROM jsonb_to_recordset({sql_lit(encoded)}::jsonb)
    AS s(slug text, external_id text, source_url text, raw_data jsonb)
), _src AS (
  SELECT b.id AS brokerage_id, s.* FROM _stage s JOIN credeals.cre_brokerages b ON b.slug = s.slug
), _essex_identity_claims AS ({incoming_claims_sql()}),
_essex_historical_identity_claims AS ({historical_claims_sql()})
SELECT (SELECT count(*) FROM credeals.cre_brokerages WHERE slug = 'essex-realty') AS brokerages,
       (SELECT count(*) FROM _stage) AS staged, (SELECT count(*) FROM _src) AS joined,
       (SELECT count(*) FROM _essex_identity_claims) AS claims,
       (SELECT count(*) FROM ({invalid_incoming_sql()}) AS invalid_claims) AS invalid,
       (SELECT count(*) FROM ({historical_conflicts_sql()}) AS historical_conflicts) AS conflicts"""


def run_probe(artifact_path, artifact_sha256, generation, producer, target_sha256, env_file):
    """Run one source-bound, read-only proof; never convert errors into holds."""
    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256 or "") or not re.fullmatch(
        r"[0-9a-f]{64}", target_sha256 or ""
    ) or not re.fullmatch(r"[0-9a-f]{40}", producer or "") or not generation:
        raise ValueError("Essex probe binding malformed")
    raw = read_file(artifact_path)
    if hashlib.sha256(raw).hexdigest() != artifact_sha256 or git_identity() != (producer, False):
        raise ValueError("Essex probe input identity changed")
    artifact = json.loads(raw)
    if (artifact.get("runMeta") or {}).get("freshness", {}).get("generationId") != generation:
        raise ValueError("Essex probe generation differs")
    rows = stage_essex_identity(artifact)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target_sha256:
        raise ValueError("Essex probe target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\n" + probe_sql(rows) + ";\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode != 0:
        raise RuntimeError("Essex read-only identity probe failed")
    conflicts = classify_probe_result(parse_tsv(result.stdout), len(rows))
    current_url, _ = load_db_url(env_file)
    if (database_target_fingerprint_from_url(current_url).get("value") != target_sha256
            or read_file(artifact_path) != raw or git_identity() != (producer, False)):
        raise ValueError("Essex probe input or target changed")
    return {"schema": "cre-essex-preseal-identity-v1", "source": "essex-realty",
            "artifact_sha256": artifact_sha256, "generation_id": generation, "producer_sha": producer,
            "database_target_sha256": target_sha256, "staged_count": len(rows),
            "staged_sha256": projection_digest(rows), "conflicts": conflicts,
            "verdict": "hold" if conflicts else "clear"}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("artifact", "artifact-sha256", "generation", "producer", "target-sha256", "out"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    result = run_probe(args.artifact, args.artifact_sha256, args.generation, args.producer,
                       args.target_sha256, args.env_file)
    fd = os.open(Path(args.out), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
