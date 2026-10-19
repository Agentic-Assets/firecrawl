"""Finite Transwestern image continuity inside the ordinary ingest transaction."""

import json

from cre_transwestern_image_roles import role_proof


def image_state_query():
    """One source-wide physical-image count/digest row, including duplicates."""
    return """SELECT count(*)::bigint AS image_count,
  encode(pg_catalog.sha256(convert_to(coalesce(jsonb_agg(
    jsonb_build_array(l.id::text,i.url,i.is_primary,i.display_order,i.alt_text)
    ORDER BY l.id,i.url COLLATE "C",i.is_primary NULLS FIRST,
             i.display_order NULLS FIRST,i.alt_text COLLATE "C" NULLS FIRST
  ),'[]'::jsonb)::text,'UTF8')),'hex') AS image_state_sha256
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
JOIN credeals.cre_listing_images i ON i.listing_id=l.id
WHERE b.slug='transwestern' AND l.deleted_at IS NULL"""


def build_parts(rows, binding):
    """Return before/project/verify fragments and an AFTER-COMMIT emit fragment."""
    from cre_ingest import sql_lit
    from cre_transwestern_image_outcome import valid_binding

    if not valid_binding(binding) or not rows:
        raise ValueError("unbound Transwestern image SQL")
    inputs, identities = [], set()
    for row in rows:
        proof = role_proof(row)
        if row["external_id"] in identities:
            raise ValueError("duplicate Transwestern staged image identity")
        identities.add(row["external_id"])
        values = [sql_lit(row[key]) for key in ("external_id", "source_url", "canonical_url")]
        values += [sql_lit(json.dumps(value)) + "::jsonb" for value in (row["images"], proof["excluded"])]
        values += [sql_lit(json.dumps(proof["primary"])) + "::jsonb" if proof["primary"] else "NULL::jsonb"]
        inputs.append("(" + ",".join(values) + ")")
    before = """CREATE TEMP TABLE _tw_image_outcome(payload jsonb) ON COMMIT PRESERVE ROWS;
CREATE TEMP TABLE _tw_image_input(
  external_id text PRIMARY KEY,source_url text,canonical_url text,
  images jsonb,excluded jsonb,witness jsonb
) ON COMMIT DROP;
INSERT INTO _tw_image_input VALUES
""" + ",\n".join(inputs) + ";\n" + r"""
DO $$ BEGIN
  IF (SELECT count(*) FROM _src) <> (SELECT count(*) FROM _tw_image_input)
     OR EXISTS (
       SELECT 1 FROM _src s JOIN credeals.cre_brokerages b ON b.id=s.brokerage_id
       LEFT JOIN _tw_image_input input USING(external_id)
       WHERE b.slug <> 'transwestern' OR input.external_id IS NULL
          OR s.source_url IS DISTINCT FROM input.source_url
          OR s.canonical_url IS DISTINCT FROM input.canonical_url
          OR s.images IS DISTINCT FROM input.images
     ) THEN RAISE EXCEPTION 'Transwestern materialized stage differs from bound rows'; END IF;
END $$;
CREATE TEMP TABLE _tw_image_parents_before ON COMMIT DROP AS
SELECT l.id AS listing_id,l.external_id,l.source_url,l.canonical_url,
       input.external_id IS NOT NULL AS staged
FROM credeals.cre_listings l
JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
LEFT JOIN _tw_image_input input ON input.external_id=l.external_id
WHERE b.slug='transwestern' AND l.deleted_at IS NULL;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM _tw_image_parents_before GROUP BY external_id HAVING count(*)>1)
     OR EXISTS (
       SELECT 1 FROM _tw_image_parents_before p JOIN _tw_image_input input USING(external_id)
       WHERE p.source_url IS DISTINCT FROM input.source_url
          OR (p.canonical_url IS NOT NULL AND p.canonical_url IS DISTINCT FROM input.canonical_url)
     ) THEN RAISE EXCEPTION 'ambiguous or changed Transwestern prior identity'; END IF;
END $$;
CREATE TEMP TABLE _tw_image_current ON COMMIT DROP AS
SELECT input.external_id,x.value->>'url' AS url,(x.ordinality-1)::int AS display_order,
       x.value AS original
FROM _tw_image_input input
CROSS JOIN LATERAL jsonb_array_elements(input.images) WITH ORDINALITY x(value,ordinality);
CREATE TEMP TABLE _tw_image_urls ON COMMIT DROP AS
SELECT external_id,url,min(display_order) AS first_order
FROM _tw_image_current GROUP BY external_id,url;
CREATE TEMP TABLE _tw_image_excluded ON COMMIT DROP AS
SELECT DISTINCT input.external_id,x->>'url' AS url
FROM _tw_image_input input CROSS JOIN LATERAL jsonb_array_elements(input.excluded) x;
CREATE TEMP TABLE _tw_images_before ON COMMIT DROP AS
SELECT i.id AS image_id,i.listing_id,i.url,i.is_primary,i.display_order,i.alt_text,p.staged,
  (p.staged AND current.url IS NULL AND (
     i.url ~ '^https://mapsresources-pa\.googleapis\.com/v1/tiles([?#].*)?$'
     OR i.url='https://transwestern.com/assets/images/transwestern-mapmarker.png'
     OR excluded.url IS NOT NULL
   )) AS removable
FROM _tw_image_parents_before p
JOIN credeals.cre_listing_images i ON i.listing_id=p.listing_id
LEFT JOIN _tw_image_urls current ON current.external_id=p.external_id AND current.url=i.url
LEFT JOIN _tw_image_excluded excluded ON excluded.external_id=p.external_id AND excluded.url=i.url;
DO $$ BEGIN
  IF (SELECT count(*) FROM _tw_images_before) <> (
    SELECT coalesce(sum(child_count),0) FROM _child_counts_by_listing_before
    WHERE source_key='transwestern' AND child_type='images'
  ) THEN RAISE EXCEPTION 'Transwestern raw image baseline mismatch'; END IF;
END $$;
"""
    project = r"""CREATE TEMP TABLE _tw_image_history ON COMMIT DROP AS
SELECT p.external_id,prior.url,min(nullif(prior.alt_text,'')) AS alt_text,
       count(DISTINCT nullif(prior.alt_text,'')) AS alt_versions
FROM _tw_images_before prior JOIN _tw_image_parents_before p USING(listing_id)
WHERE prior.staged GROUP BY p.external_id,prior.url;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM _tw_images_before WHERE staged AND is_primary IS TRUE
    GROUP BY listing_id HAVING count(*)>1
  ) THEN RAISE EXCEPTION 'ambiguous Transwestern prior primaries'; END IF;
  IF EXISTS (
    SELECT 1 FROM _tw_image_history history JOIN _tw_image_urls USING(external_id,url)
    WHERE history.alt_versions>1
  ) THEN RAISE EXCEPTION 'ambiguous Transwestern useful alt text'; END IF;
END $$;
CREATE TEMP TABLE _tw_image_choice ON COMMIT DROP AS
SELECT input.external_id,
  CASE WHEN input.witness IS NOT NULL THEN input.witness->>'url'
       WHEN retained.url IS NOT NULL
       THEN old.url ELSE NULL END AS primary_url,
  CASE WHEN input.witness IS NOT NULL THEN 'current_source_primary'
       WHEN retained.url IS NOT NULL
       THEN 'historical_primary' ELSE 'unassigned_primary' END AS primary_basis
FROM _tw_image_input input
LEFT JOIN _tw_image_parents_before p USING(external_id)
LEFT JOIN _tw_images_before old ON old.listing_id=p.listing_id AND old.is_primary IS TRUE
LEFT JOIN _tw_image_urls retained ON retained.external_id=input.external_id AND retained.url=old.url;
CREATE TEMP TABLE _tw_image_projection ON COMMIT DROP AS
SELECT current.external_id,current.url,current.display_order,
  (current.url=choice.primary_url AND current.display_order=urls.first_order) IS TRUE AS is_primary,
  history.alt_text,current.original
FROM _tw_image_current current JOIN _tw_image_choice choice USING(external_id)
JOIN _tw_image_urls urls USING(external_id,url)
LEFT JOIN _tw_image_history history USING(external_id,url);
WITH grouped AS (
  SELECT external_id,jsonb_agg(projection.original || jsonb_build_object(
    'isPrimary',projection.is_primary,'order',projection.display_order,'altText',projection.alt_text
  ) ORDER BY projection.display_order) AS images
  FROM _tw_image_projection projection GROUP BY external_id
)
UPDATE _src s SET images=coalesce(grouped.images,'[]'::jsonb)
FROM _tw_image_input input LEFT JOIN grouped USING(external_id)
WHERE s.slug='transwestern' AND s.external_id=input.external_id;
"""
    verify = r"""DO $$ BEGIN
  IF EXISTS (
    SELECT p.listing_id FROM _tw_image_parents_before p
    EXCEPT SELECT listing_id FROM _retained_child_scope_after WHERE source_key='transwestern'
  ) THEN RAISE EXCEPTION 'Transwestern transition retired a prior parent'; END IF;
  IF EXISTS (
    SELECT prior.listing_id,prior.url FROM _tw_images_before prior WHERE NOT removable
    EXCEPT ALL
    SELECT i.listing_id,i.url FROM credeals.cre_listing_images i
    JOIN _retained_child_scope_after a ON a.listing_id=i.listing_id WHERE a.source_key='transwestern'
  ) THEN RAISE EXCEPTION 'Transwestern protected image multiplicity lost'; END IF;
  IF EXISTS (
    (SELECT listing_id,url,is_primary,display_order,alt_text FROM _tw_images_before WHERE NOT staged
     EXCEPT ALL
     SELECT i.listing_id,i.url,i.is_primary,i.display_order,i.alt_text
     FROM credeals.cre_listing_images i JOIN _tw_image_parents_before p USING(listing_id) WHERE NOT p.staged)
    UNION ALL
    (SELECT i.listing_id,i.url,i.is_primary,i.display_order,i.alt_text
     FROM credeals.cre_listing_images i JOIN _tw_image_parents_before p USING(listing_id) WHERE NOT p.staged
     EXCEPT ALL SELECT listing_id,url,is_primary,display_order,alt_text FROM _tw_images_before WHERE NOT staged)
  ) THEN RAISE EXCEPTION 'Transwestern offstage image state changed'; END IF;
  IF EXISTS (
    (SELECT p.external_id,p.url,p.is_primary,p.display_order,p.alt_text FROM _tw_image_projection p
     EXCEPT ALL
     SELECT l.external_id,i.url,i.is_primary,i.display_order,i.alt_text
     FROM credeals.cre_listing_images i JOIN credeals.cre_listings l ON l.id=i.listing_id
     JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
     JOIN _tw_image_input input ON input.external_id=l.external_id
     WHERE b.slug='transwestern' AND l.deleted_at IS NULL)
    UNION ALL
    (SELECT l.external_id,i.url,i.is_primary,i.display_order,i.alt_text
     FROM credeals.cre_listing_images i JOIN credeals.cre_listings l ON l.id=i.listing_id
     JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
     JOIN _tw_image_input input ON input.external_id=l.external_id
     WHERE b.slug='transwestern' AND l.deleted_at IS NULL
     EXCEPT ALL SELECT p.external_id,p.url,p.is_primary,p.display_order,p.alt_text FROM _tw_image_projection p)
  ) THEN RAISE EXCEPTION 'Transwestern actual image projection mismatch'; END IF;
  IF EXISTS (
    SELECT 1 FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
    LEFT JOIN _tw_image_input input ON input.external_id=l.external_id
    LEFT JOIN _tw_image_parents_before p ON p.listing_id=l.id
    WHERE b.slug='transwestern' AND l.deleted_at IS NULL AND p.listing_id IS NULL AND input.external_id IS NULL
  ) THEN RAISE EXCEPTION 'unexpected Transwestern active parent'; END IF;
END $$;
INSERT INTO _tw_image_outcome(payload)
SELECT jsonb_build_object(
  'schema','transwestern-image-transition-v1','source_key','transwestern',
  'binding',BINDING,
  'raw_before',(SELECT count(*) FROM _tw_images_before),
  'raw_retained_after',(SELECT count(*) FROM credeals.cre_listing_images i JOIN _tw_image_parents_before p USING(listing_id)),
  'raw_new_after',state.image_count-(SELECT count(*) FROM credeals.cre_listing_images i JOIN _tw_image_parents_before p USING(listing_id)),
  'raw_after',state.image_count,'raw_retired_before',0,
  'protected_before',(SELECT count(*) FROM _tw_images_before WHERE NOT removable),
  'protected_retained_after',(SELECT count(*) FROM (
    SELECT listing_id,url FROM _tw_images_before WHERE NOT removable
    INTERSECT ALL
    SELECT i.listing_id,i.url FROM credeals.cre_listing_images i
    JOIN _retained_child_scope_after a ON a.listing_id=i.listing_id WHERE a.source_key='transwestern'
  ) protected),
  'excluded_before',(SELECT count(*) FROM _tw_images_before WHERE removable),
  'staged_current',(SELECT count(*) FROM _tw_image_input),
  'current_source_primary',(SELECT count(*) FROM _tw_image_choice WHERE primary_basis='current_source_primary'),
  'historical_primary',(SELECT count(*) FROM _tw_image_choice WHERE primary_basis='historical_primary'),
  'unassigned_primary',(SELECT count(*) FROM _tw_image_choice WHERE primary_basis='unassigned_primary'),
  'image_state_sha256',state.image_state_sha256
) FROM (IMAGE_STATE_QUERY) state;
""".replace("BINDING", sql_lit(json.dumps(binding)) + "::jsonb").replace("IMAGE_STATE_QUERY", image_state_query())
    emit = r"""\pset format unaligned
\pset tuples_only on
SELECT 'CRE_TRANSWESTERN_IMAGE_TRANSITION ' || payload::text FROM _tw_image_outcome;
\pset format aligned
\pset tuples_only off
"""
    return {"before": before, "project": project, "verify": verify, "emit": emit}
