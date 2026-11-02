-- 021_cre_market_summary_structure_gate_readback.sql (AGENTIC-5129). Read only.
-- Run before 021 (old values) and after it (new values). Expected after apply,
-- with GetCREdata lsc-2026.11.1 applied and backfilled:
--   columns           the twelve prior columns unchanged, median_price_per_sf numeric last
--   security_invoker  true; acl postgres and service_role only
--   rpc_rows          get_cre_market_summary('{}') returns rows (it is STABLE)
--   psf_outside_band  0 (no market average or median outside $0.10 to $3,000)
--   dc_other          avg_price_per_sf = median_price_per_sf = 78.43, avg_price
--                     without the $9.9e37 placeholder
--   islandia_industrial  avg_price_per_sf and median_price_per_sf null
-- Before apply, dc_other.avg_price is about 2.475e37 and islandia_industrial
-- shows the stored 52,742,048 average.
begin read only;
set local statement_timeout = '2min';

select jsonb_build_object(
  'columns', (select jsonb_agg(jsonb_build_object('name', a.attname, 'type', format_type(a.atttypid, a.atttypmod)) order by a.attnum)
                from pg_attribute a
               where a.attrelid = 'credeals.v_cre_market_summary'::regclass and a.attnum > 0 and not a.attisdropped),
  'security_invoker', (select coalesce('security_invoker=true' = any(c.reloptions), false) from pg_class c
                        where c.oid = 'credeals.v_cre_market_summary'::regclass),
  'acl', (select c.relacl::text from pg_class c where c.oid = 'credeals.v_cre_market_summary'::regclass),
  'definition_md5', md5(pg_get_viewdef('credeals.v_cre_market_summary'::regclass, true)),
  'reads_structure', position('v_cre_listing_structure_current' in pg_get_viewdef('credeals.v_cre_market_summary'::regclass, true)) > 0,
  'rpc_rows', (select count(*) from credeals.get_cre_market_summary('{}'::jsonb)),
  'markets', (select count(*) from credeals.v_cre_market_summary),
  'markets_with_psf', (select count(*) from credeals.v_cre_market_summary where avg_price_per_sf is not null),
  'psf_outside_band', (select count(*) from credeals.v_cre_market_summary
                        where avg_price_per_sf not between 0.10 and 3000),
  'max_avg_price', (select max(avg_price)::text from credeals.v_cre_market_summary),
  'max_avg_price_per_sf', (select max(avg_price_per_sf) from credeals.v_cre_market_summary),
  'dc_other', (select to_jsonb(m) - 'city' - 'state' - 'property_type' from credeals.v_cre_market_summary m
                where m.city = 'Washington' and m.state = 'DC' and m.property_type = 'other'),
  'islandia_industrial', (select to_jsonb(m) - 'city' - 'state' - 'property_type' from credeals.v_cre_market_summary m
                           where m.city = 'Islandia' and m.state = 'NY' and m.property_type = 'industrial'));

rollback;
