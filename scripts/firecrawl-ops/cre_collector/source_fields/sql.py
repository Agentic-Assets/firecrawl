"""Fixed, in-transaction updates of source-owned listing fields."""

RESERVED_KEY = "_source_field_provenance"


def build_field_updates():
    """Use the same ownership and clock rule for the three approved fields."""
    return "\n".join(
        _field_update(*field)
        for field in (
            ("available_space_count", "integer", "s.source_field_count", "lease"),
            (
                "elevator_count",
                "integer",
                "s.source_field_building->'elevator_count'",
                "property",
            ),
            (
                "typical_floor_size_text",
                "text",
                "s.source_field_building->'typical_floor_size_text'",
                "property",
            ),
        )
    )


def _field_update(column, sql_type, candidate, tenure):
    # Identifiers and expressions come exclusively from the fixed tuple above.
    root = "{_source_field_provenance}"
    prior = "{_source_field_provenance," + column + "}"

    def leaf(name):
        return "{_source_field_provenance," + column + "," + name + "}"

    return f"""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='credeals' AND table_name='cre_listings'
               AND column_name='{column}' AND data_type='{sql_type}')
     AND EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema='credeals' AND table_name='cre_listings'
                   AND column_name='extra_facts' AND data_type='jsonb') THEN
    UPDATE credeals.cre_listings t
    SET {column} = (({candidate})->>'value')::{sql_type},
        extra_facts = jsonb_set(COALESCE(t.extra_facts, '{{}}'::jsonb),
          '{root}', COALESCE(t.extra_facts->'{RESERVED_KEY}', '{{}}'::jsonb) ||
            jsonb_build_object('{column}', ({candidate})), true)
    FROM _src s
    WHERE t.brokerage_id=s.brokerage_id AND t.external_id=s.external_id
      AND jsonb_typeof(({candidate}))='object'
      AND ({candidate})->>'schema_version'='cre-source-field-observation/v1'
      AND ({candidate})->>'field'='{column}' AND ({candidate})->>'tenure'='{tenure}'
      AND (t.extra_facts IS NULL OR jsonb_typeof(t.extra_facts)='object')
      AND (t.extra_facts->'{RESERVED_KEY}' IS NULL OR jsonb_typeof(t.extra_facts->'{RESERVED_KEY}')='object')
      AND (
        (t.{column} IS NULL AND t.extra_facts#>'{prior}' IS NULL)
        OR (
          jsonb_typeof(t.extra_facts#>'{prior}')='object'
          AND t.extra_facts#>>'{leaf("schema_version")}'='cre-source-field-observation/v1'
          AND t.extra_facts#>>'{leaf("field")}'='{column}'
          AND t.extra_facts#>>'{leaf("source_key")}'=({candidate})->>'source_key'
          AND t.extra_facts#>>'{leaf("native_id")}'=({candidate})->>'native_id'
          AND t.extra_facts#>>'{leaf("url")}'=({candidate})->>'url'
          AND t.extra_facts#>>'{leaf("tenure")}'='{tenure}'
          AND t.extra_facts#>>'{leaf("value")}'=t.{column}::text
          AND t.extra_facts#>>'{leaf("observed_at")}' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}}\\.\\d{{6}}Z$'
          AND t.extra_facts#>>'{leaf("observed_at")}' COLLATE "C" < (({candidate})->>'observed_at') COLLATE "C"
        )
      );
  END IF;
END $$;
"""
