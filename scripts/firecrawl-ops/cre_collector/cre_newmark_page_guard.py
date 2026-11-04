"""One-item Newmark page identity check under the ingest transaction lock."""
import re
import uuid
from datetime import datetime

PREFIX = "CRE_NEWMARK_PAGE_V1 "


def guard_sql(attempt, claim, *, require_page=True):
    if not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{64}", attempt):
        raise ValueError("invalid Newmark page attempt")
    if (not isinstance(claim, dict) or set(claim) != {"id", "claimedAt", "attempts", "brokerageId", "externalId", "url"}
            or not isinstance(claim["id"], str) or not isinstance(claim["brokerageId"], str)
            or type(claim["attempts"]) is not int or claim["attempts"] < 0
            or not all(isinstance(claim[k], str) and claim[k] for k in ("claimedAt", "externalId", "url"))):
        raise ValueError("invalid Newmark page claim")
    try:
        uuid.UUID(claim["id"])
        uuid.UUID(claim["brokerageId"])
        if datetime.fromisoformat(claim["claimedAt"]).tzinfo is None:
            raise ValueError("claim timestamp lacks timezone")
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid Newmark page claim identity") from exc
    def lit(value):
        return "'" + value.replace("'", "''") + "'"
    locked = (
        "WITH newmark_locked_claim AS MATERIALIZED (SELECT * FROM credeals.cre_enrichment_queue q WHERE "
        + "q.id=" + lit(claim["id"]) + "::uuid AND q.brokerage_id=" + lit(claim["brokerageId"]) + "::uuid"
        + " AND q.source_key='newmark' AND q.external_id=" + lit(claim["externalId"])
        + " AND q.url=" + lit(claim["url"]) + " AND q.claimed_at=" + lit(claim["claimedAt"]) + "::timestamptz"
        + " AND q.attempts=" + str(claim["attempts"]) + " AND q.done_at IS NULL FOR UPDATE)\n"
    )
    if not require_page:
        return locked + r"""
SELECT NOT (
  (SELECT count(*) FROM _src) = 1
  AND EXISTS (
    SELECT 1 FROM _src s
    JOIN credeals.cre_brokerages b ON b.id=s.brokerage_id AND b.slug='newmark'
    JOIN newmark_locked_claim q ON q.brokerage_id=s.brokerage_id
      AND q.external_id=s.external_id AND q.url=s.source_url
    JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id
      AND l.external_id=s.external_id AND l.source_url=s.source_url
    WHERE (SELECT count(*) FROM credeals.cre_listings other
           WHERE other.brokerage_id=s.brokerage_id AND other.source_url=s.source_url)=1
  )
) AS newmark_page_refused \gset
\if :newmark_page_refused
ROLLBACK;
\pset format unaligned
\pset tuples_only on
SELECT 'CRE_NEWMARK_PAGE_V1 ATTEMPT refused';
\quit
\endif
""".replace("ATTEMPT", attempt)
    return locked + r"""
SELECT NOT (
  (SELECT count(*) FROM _src) = 1
  AND EXISTS (
    SELECT 1 FROM _src s
    JOIN credeals.cre_brokerages b ON b.id=s.brokerage_id AND b.slug='newmark'
    JOIN newmark_locked_claim q ON q.brokerage_id=s.brokerage_id
      AND q.external_id=s.external_id AND q.url=s.source_url
    JOIN credeals.cre_listings l ON l.brokerage_id=s.brokerage_id
      AND l.external_id=s.external_id AND l.source_url=s.source_url
    CROSS JOIN LATERAL (SELECT s.raw_data->'newmarkPageObservation'->'witness' AS w) p
    CROSS JOIN LATERAL (SELECT CASE
      WHEN l.raw_data ? 'rawNewmarkNimRecord'
       AND l.raw_data->'latestInventoryObservation' ? 'rawNewmarkNimRecord'
       AND l.raw_data->'rawNewmarkNimRecord' IS DISTINCT FROM
           l.raw_data->'latestInventoryObservation'->'rawNewmarkNimRecord' THEN NULL
      ELSE COALESCE(l.raw_data->'rawNewmarkNimRecord',
                    l.raw_data->'latestInventoryObservation'->'rawNewmarkNimRecord') END AS r) n
    WHERE p.w IS NOT NULL
      AND p.w->>'recordId'=n.r->>'id' AND p.w->>'slug'=n.r->>'slug'
      AND p.w->>'sourceUrl'=s.source_url AND p.w->>'sourceUrl'=n.r->>'externalWebsiteUrl'
      AND CASE WHEN jsonb_typeof(n.r->'properties')='array'
               THEN jsonb_array_length(n.r->'properties')=1 ELSE false END
      AND p.w->>'address'=n.r->'properties'->0->>'address'
      AND p.w->>'city'=n.r->'properties'->0->>'city'
      AND p.w->>'state'=n.r->'properties'->0->>'stateDescription'
      AND p.w->>'zip'=n.r->'properties'->0->>'zip'
      AND (SELECT count(*) FROM credeals.cre_listings other
           WHERE other.brokerage_id=s.brokerage_id AND other.source_url=s.source_url)=1
  )
) AS newmark_page_refused \gset
\if :newmark_page_refused
ROLLBACK;
\pset format unaligned
\pset tuples_only on
SELECT 'CRE_NEWMARK_PAGE_V1 ATTEMPT refused';
\quit
\endif
""".replace("ATTEMPT", attempt)


def commit_sql(attempt):
    if not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{64}", attempt):
        raise ValueError("invalid Newmark page attempt")
    return "\\pset format unaligned\n\\pset tuples_only on\nSELECT 'CRE_NEWMARK_PAGE_V1 " + attempt + " committed';\n"


def parse_output(text, attempt):
    if not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{64}", attempt):
        raise ValueError("invalid Newmark page attempt")
    matches = [line for line in text.splitlines(keepends=True) if line.startswith(PREFIX)]
    if len(matches) != 1 or matches[0] not in {
        PREFIX + attempt + " refused\n", PREFIX + attempt + " committed\n",
    }:
        raise ValueError("missing or ambiguous Newmark page outcome")
    return matches[0].rstrip("\n").rsplit(" ", 1)[1]
