"""In-transaction, source-owned listing field update."""

RESERVED_KEY = "_source_field_provenance"


def build_count_update():
    return """
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='credeals' AND table_name='cre_listings'
               AND column_name='available_space_count' AND data_type='integer')
     AND EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema='credeals' AND table_name='cre_listings'
                   AND column_name='extra_facts' AND data_type='jsonb') THEN
    UPDATE credeals.cre_listings t
    SET available_space_count = (s.source_field_count->>'value')::integer,
        extra_facts = jsonb_set(COALESCE(t.extra_facts, '{}'::jsonb),
          '{_source_field_provenance}',
          COALESCE(t.extra_facts->'_source_field_provenance', '{}'::jsonb) ||
            jsonb_build_object('available_space_count', s.source_field_count), true)
    FROM _src s
    WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.external_id
      AND s.source_field_count IS NOT NULL
      AND (t.extra_facts IS NULL OR jsonb_typeof(t.extra_facts)='object')
      AND (t.extra_facts->'_source_field_provenance' IS NULL OR
           jsonb_typeof(t.extra_facts->'_source_field_provenance')='object')
      AND (
        (t.available_space_count IS NULL AND
         t.extra_facts#>'{_source_field_provenance,available_space_count}' IS NULL)
        OR (
          jsonb_typeof(t.extra_facts#>'{_source_field_provenance,available_space_count}')='object'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,schema_version}' = 'cre-source-field-observation/v1'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,field}' = 'available_space_count'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,source_key}' = s.source_field_count->>'source_key'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,native_id}' = s.source_field_count->>'native_id'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,url}' = s.source_field_count->>'url'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,tenure}' = 'lease'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,value}' = t.available_space_count::text
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,observed_at}' ~ '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$'
          AND t.extra_facts#>>'{_source_field_provenance,available_space_count,observed_at}' COLLATE "C" < (s.source_field_count->>'observed_at') COLLATE "C"
        )
      );
  END IF;
END $$;
"""
