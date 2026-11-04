-- 021_cre_market_summary_structure_gate_readback.sql (AGENTIC-5129). Read only.
-- Run before 021 (old values) and after it (new values). Expected after apply,
-- with GetCREdata lsc-2026.11.1 applied and backfilled:
--   columns              the twelve prior columns unchanged, then median_price_per_sf
--                        numeric, price_per_sf_n bigint, cap_rate_n bigint,
--                        median_size_sf numeric, size_sf_n bigint
--   security_invoker     true; acl postgres and service_role only
--   reads_structure, reads_leg_gate  true
--   rpc_rows             get_cre_market_summary('{}') returns rows (it is STABLE)
--                        and rpc_has_n_columns is true
--   medians_below_floor  0 (no average or median published below 3 values)
--   n_mismatch           0 for each leg: the view's n equals an independent count
--                        from the base rows (leg gate, psf_allowed and band for
--                        $/SF; leg gate for cap rate; for-sale building, portfolio
--                        or land area for size), every market compared, including
--                        those with a null property type
--   max_avg_price        under 1,000,000,000 (D-8 removed the placeholders)
--   anchors              as recorded on the pull request; DC "other" and Islandia
--                        NY industrial have no $/SF median (n 1 and 0), and after
--                        D-8 DC "other" has cap_rate_n 2 and no cap median (its
--                        low-ratio listing c1f700b1 leaves the cap leg)
-- Informational: ratios_outside_band and psf_allowed_but_gated count the rows
-- the band and the leg gate keep out. Before apply the n columns are absent, so
-- the n checks report null and the anchors show the old values.
begin read only;
set local statement_timeout = '5min';

with base as (
    select l.city, l.state, l.property_type,
           s.psf_allowed and l.sale_price_usd > 0 and l.size_sf > 0
               and credeals.cre_listing_market_evidence_exclusion_v1('price_per_sf', l.transaction_type,
                       s.interest_kind, s.asset_scope, s.price_basis, s.size_basis, s.structure_tags,
                       s.structure_method->>'price_basis', s.structure_method->>'size_basis') is null as psf_eligible,
           s.psf_allowed and l.sale_price_usd > 0 and l.size_sf > 0 as psf_allowed,
           l.sale_price_usd / nullif(l.size_sf, 0) as ratio,
           l.property_type = 'land' or s.size_basis = 'land_area' as is_land,
           l.cap_rate is not null
               and credeals.cre_listing_market_evidence_exclusion_v1('cap_rate', l.transaction_type,
                       s.interest_kind, s.asset_scope, s.price_basis, s.size_basis, s.structure_tags,
                       s.structure_method->>'price_basis', s.structure_method->>'size_basis') is null as cap_eligible,
           l.size_sf > 0 and l.transaction_type in ('sale', 'sale_or_lease')
               and s.size_basis in ('building_area', 'portfolio_total_area', 'land_area') as size_eligible
      from credeals.cre_listings l
      left join credeals.v_cre_listing_structure_current s on s.listing_id = l.id
     where l.deleted_at is null
       and l.status in ('active', 'under_contract', 'pending')
       and l.city is not null
       and l.state is not null
), expected as (
    select city, state, property_type,
           count(*) filter (where psf_eligible and ratio between case when is_land then 0.10 else 5 end
                                                          and case when is_land then 1000 else 3000 end) as price_per_sf_n,
           count(*) filter (where cap_eligible) as cap_rate_n,
           count(*) filter (where size_eligible) as size_sf_n,
           count(*) filter (where psf_eligible and not coalesce(ratio between case when is_land then 0.10 else 5 end
                                                                      and case when is_land then 1000 else 3000 end, false))
             as ratios_outside_band,
           count(*) filter (where psf_allowed and not coalesce(psf_eligible, false)) as psf_allowed_but_gated
      from base
     group by city, state, property_type
), published as (
    select m.city, m.state, m.property_type, to_jsonb(m) as j from credeals.v_cre_market_summary m
)
select jsonb_build_object(
  'columns', (select jsonb_agg(jsonb_build_object('name', a.attname, 'type', format_type(a.atttypid, a.atttypmod)) order by a.attnum)
                from pg_attribute a
               where a.attrelid = 'credeals.v_cre_market_summary'::regclass and a.attnum > 0 and not a.attisdropped),
  'security_invoker', (select coalesce('security_invoker=true' = any(c.reloptions), false) from pg_class c
                        where c.oid = 'credeals.v_cre_market_summary'::regclass),
  'acl', (select c.relacl::text from pg_class c where c.oid = 'credeals.v_cre_market_summary'::regclass),
  'definition_md5', md5(pg_get_viewdef('credeals.v_cre_market_summary'::regclass, true)),
  'reads_structure', position('v_cre_listing_structure_current' in pg_get_viewdef('credeals.v_cre_market_summary'::regclass, true)) > 0,
  'reads_leg_gate', position('cre_listing_market_evidence_exclusion_v1' in pg_get_viewdef('credeals.v_cre_market_summary'::regclass, true)) > 0,
  'rpc_rows', (select count(*) from credeals.get_cre_market_summary('{}'::jsonb)),
  'rpc_has_n_columns', (select to_jsonb(r) ? 'price_per_sf_n' from credeals.get_cre_market_summary('{}'::jsonb) r limit 1),
  'markets', (select count(*) from published),
  'markets_with_psf_median', (select count(*) from published where j->>'median_price_per_sf' is not null),
  'markets_with_cap_median', (select count(*) from published where j->>'median_cap_rate' is not null),
  'markets_with_size_median', (select count(*) from published where j->>'median_size_sf' is not null),
  'medians_below_floor', (select count(*) from published
                           where ((j->>'median_price_per_sf' is not null or j->>'avg_price_per_sf' is not null)
                                  and coalesce((j->>'price_per_sf_n')::bigint, 0) < 3)
                              or (j->>'median_cap_rate' is not null and coalesce((j->>'cap_rate_n')::bigint, 0) < 3)
                              or (j->>'median_size_sf' is not null and coalesce((j->>'size_sf_n')::bigint, 0) < 3)),
  'n_mismatch', (select case when bool_or(p.j ? 'price_per_sf_n') then jsonb_build_object(
                     'price_per_sf', count(*) filter (where (p.j->>'price_per_sf_n')::bigint is distinct from e.price_per_sf_n),
                     'cap_rate', count(*) filter (where (p.j->>'cap_rate_n')::bigint is distinct from e.cap_rate_n),
                     'size_sf', count(*) filter (where (p.j->>'size_sf_n')::bigint is distinct from e.size_sf_n)) end
                   from published p
                   join expected e on e.city = p.city and e.state = p.state
                                  and e.property_type is not distinct from p.property_type),
  'ratios_outside_band', (select sum(ratios_outside_band) from expected),
  'psf_allowed_but_gated', (select sum(psf_allowed_but_gated) from expected),
  'max_avg_price', (select max((j->>'avg_price')::numeric)::text from published),
  'max_median_price_per_sf', (select max((j->>'median_price_per_sf')::numeric) from published),
  'anchors', (select jsonb_object_agg(city || ' ' || state || ' ' || property_type,
                     j - 'city' - 'state' - 'property_type' - 'last_scraped_at')
                from published
               where (city, state, property_type) in (('Houston', 'TX', 'industrial'), ('Austin', 'TX', 'retail'),
                                                      ('Phoenix', 'AZ', 'retail'), ('Washington', 'DC', 'other'),
                                                      ('Islandia', 'NY', 'industrial'))));

rollback;
