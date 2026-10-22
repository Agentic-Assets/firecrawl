WITH base AS (
  SELECT
    
CASE
  WHEN b.slug = 'cbre'
    AND l.external_id LIKE 'dealflow:%' THEN 'cbre-dealflow'
  WHEN b.slug = 'jll'
    AND l.external_id LIKE 'investor:%' THEN 'jll-investor'
  WHEN b.slug = 'colliers'
    AND l.external_id LIKE 'main:%' THEN 'colliers-main'
  WHEN b.slug IN ('33-realty', 'avison-young', 'berger-commercial', 'bradford-allen', 'bull-realty', 'cawley-chicago', 'cushman-wakefield', 'daum-commercial', 'essex-realty', 'faris-lee', 'fortis-net-lease', 'foundry-commercial', 'franklin-street', 'gibson-commercial', 'greysteel', 'hanley', 'hudson-peters', 'interra-realty', 'kidder-mathews', 'kiser-group', 'lee-associates', 'leibsohn', 'lyon-stahl', 'marcus-millichap', 'matthews', 'metro-commercial', 'nai-bergman', 'nai-dominion', 'nai-global', 'nai-hallmark', 'nai-hiffman', 'nai-isaac', 'nai-martens', 'nai-plotkin', 'nai-talcor', 'newmark', 'pinnacle-rea', 'pyramid-brokerage', 'savills', 'srs', 'svn', 'transwestern', 'tri-commercial', 'trinity-partners', 'unique-properties') THEN b.slug
  ELSE COALESCE(
    (SELECT btrim(candidate.source_key)
       FROM (VALUES
         (1, l.raw_data #>> '{latestInventoryObservation,sourceKey}'),
         (2, l.raw_data #>> '{latestInventoryObservation,primary,sourceKey}'),
         (3, l.raw_data #>> '{latestInventoryObservation,secondary_pass,sourceKey}'),
         (4, l.raw_data->>'sourceKey'),
         (5, l.raw_data #>> '{primary,sourceKey}'),
         (6, l.raw_data #>> '{secondary_pass,sourceKey}')
       ) AS candidate(priority, source_key)
       WHERE NULLIF(btrim(candidate.source_key), '') IS NOT NULL
         AND b.slug IN ('33-realty', 'avison-young', 'berger-commercial', 'bradford-allen', 'bull-realty', 'cawley-chicago', 'cbre', 'colliers', 'cushman-wakefield', 'daum-commercial', 'essex-realty', 'faris-lee', 'fortis-net-lease', 'foundry-commercial', 'franklin-street', 'gibson-commercial', 'greysteel', 'hanley', 'hudson-peters', 'interra-realty', 'jll', 'kidder-mathews', 'kiser-group', 'lee-associates', 'leibsohn', 'lyon-stahl', 'marcus-millichap', 'matthews', 'metro-commercial', 'nai-bergman', 'nai-dominion', 'nai-global', 'nai-hallmark', 'nai-hiffman', 'nai-isaac', 'nai-martens', 'nai-plotkin', 'nai-talcor', 'newmark', 'pinnacle-rea', 'pyramid-brokerage', 'savills', 'srs', 'svn', 'transwestern', 'tri-commercial', 'trinity-partners', 'unique-properties')
         AND (
          btrim(candidate.source_key) = b.slug
          OR (b.slug = 'cbre' AND btrim(candidate.source_key) = 'cbre-dealflow')
          OR (b.slug = 'jll' AND btrim(candidate.source_key) = 'jll-investor')
          OR (b.slug = 'colliers' AND btrim(candidate.source_key) = 'colliers-main')
         )
       ORDER BY candidate.priority
       LIMIT 1),
    CASE WHEN b.slug IN ('33-realty', 'avison-young', 'berger-commercial', 'bradford-allen', 'bull-realty', 'cawley-chicago', 'cbre', 'colliers', 'cushman-wakefield', 'daum-commercial', 'essex-realty', 'faris-lee', 'fortis-net-lease', 'foundry-commercial', 'franklin-street', 'gibson-commercial', 'greysteel', 'hanley', 'hudson-peters', 'interra-realty', 'jll', 'kidder-mathews', 'kiser-group', 'lee-associates', 'leibsohn', 'lyon-stahl', 'marcus-millichap', 'matthews', 'metro-commercial', 'nai-bergman', 'nai-dominion', 'nai-global', 'nai-hallmark', 'nai-hiffman', 'nai-isaac', 'nai-martens', 'nai-plotkin', 'nai-talcor', 'newmark', 'pinnacle-rea', 'pyramid-brokerage', 'savills', 'srs', 'svn', 'transwestern', 'tri-commercial', 'trinity-partners', 'unique-properties') THEN b.slug END
  )
END
 AS source_key,
    b.slug AS brokerage_slug,
    l.transaction_type,
    l.scraped_at,
    NULLIF(decoded.raw_data->>'inventoryObservedAt', '')::timestamptz
      AS inventory_observed_at,
    jsonb_path_exists(decoded.raw_data, '$.**.detailUnavailable') AS detail_unavailable,
    l.deleted_at
  FROM credeals.cre_listings l
  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id
  CROSS JOIN LATERAL (
    SELECT l.external_id, l.raw_data #> '{}'::text[] AS raw_data
    OFFSET 0
  ) AS decoded
),
active AS (
  SELECT * FROM base WHERE deleted_at IS NULL
),
latest AS (
  SELECT source_key,
         max(scraped_at) AS latest_scraped_at,
         max(inventory_observed_at) AS latest_inventory_observed_at
  FROM active
  GROUP BY source_key
),
soft_deleted AS (
  SELECT source_key, count(*) AS soft_deleted
  FROM base
  WHERE deleted_at IS NOT NULL
  GROUP BY source_key
)
SELECT
  a.source_key,
  min(a.brokerage_slug) AS brokerage_slug,
  count(*)::text AS active,
  count(*) FILTER (WHERE a.transaction_type = 'sale')::text AS sale,
  count(*) FILTER (WHERE a.transaction_type = 'lease')::text AS lease,
  count(*) FILTER (WHERE a.transaction_type = 'sale_or_lease')::text AS sale_or_lease,
  count(*) FILTER (WHERE a.scraped_at = latest.latest_scraped_at)::text AS latest_batch_active,
  to_char(latest.latest_scraped_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS"Z"') AS latest_scraped_at,
  count(*) FILTER (
    WHERE a.inventory_observed_at = latest.latest_inventory_observed_at
  )::text AS latest_inventory_batch_active,
  to_char(
    latest.latest_inventory_observed_at AT TIME ZONE 'UTC',
    'YYYY-MM-DD HH24:MI:SS"Z"'
  ) AS latest_inventory_observed_at,
  count(*) FILTER (WHERE a.detail_unavailable)::text AS detail_unavailable,
  coalesce(max(soft_deleted.soft_deleted), 0)::text AS soft_deleted
FROM active a
JOIN latest ON latest.source_key = a.source_key
LEFT JOIN soft_deleted ON soft_deleted.source_key = a.source_key
GROUP BY a.source_key, latest.latest_scraped_at, latest.latest_inventory_observed_at
ORDER BY a.source_key;
