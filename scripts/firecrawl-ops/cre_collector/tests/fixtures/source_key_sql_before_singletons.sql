CASE
  WHEN b.slug = 'cbre'
    AND l.external_id LIKE 'dealflow:%' THEN 'cbre-dealflow'
  WHEN b.slug = 'jll'
    AND l.external_id LIKE 'investor:%' THEN 'jll-investor'
  WHEN b.slug = 'colliers'
    AND l.external_id LIKE 'main:%' THEN 'colliers-main'
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
