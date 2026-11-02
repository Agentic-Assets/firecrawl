-- Verbatim pg_get_functiondef of credeals.cre_listing_market_evidence_exclusion_v1, read
-- 2026-11-02 from the shared project (read only). Owner: GetCREdata
-- sql/migrations/20261101_listing_structure_market_evidence.sql (AGENTIC-5127).
-- test_cre_market_summary_view.py checks the body below still hashes to the
-- recorded md5 a7e0b5a8ac36e8bdc513845b7348fcab; refresh it from the live
-- definition, never by hand.
CREATE OR REPLACE FUNCTION credeals.cre_listing_market_evidence_exclusion_v1(p_leg text, p_transaction_type text, p_interest_kind text, p_asset_scope text, p_price_basis text, p_size_basis text, p_structure_tags text[], p_price_basis_method text, p_size_basis_method text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
 SET search_path TO ''
AS $function$
    select case
        when p_leg is null or p_leg not in ('cap_rate', 'price_per_sf') then 'structure_unresolved'
        when lower(btrim(coalesce(p_transaction_type, ''))) = 'lease' then 'price_not_stated'
        when p_interest_kind is null or p_asset_scope is null
          or p_price_basis is null or p_size_basis is null then 'structure_unresolved'
        when p_interest_kind not in ('fee_simple', 'leased_fee', 'leasehold', 'partial_interest',
                                     'debt_position', 'capital_solicitation', 'business_only',
                                     'unstated', 'unresolved')
          or p_asset_scope not in ('single_asset', 'portfolio', 'portfolio_component',
                                   'unit_within_property', 'multiple_units', 'unstated', 'unresolved')
          or p_price_basis not in ('property_price', 'interest_price', 'debt_price',
                                   'debt_face_or_upb', 'debt_payoff_or_judgment', 'raise_amount',
                                   'starting_bid', 'reserve_price', 'property_and_business_price',
                                   'business_price', 'unresolved', 'not_applicable')
          or p_size_basis not in ('building_area', 'portfolio_total_area', 'unit_area',
                                  'avg_unit_area', 'land_area', 'tenant_improvement_area',
                                  'collateral_building_area', 'unresolved', 'not_applicable')
            then 'structure_unresolved'
        when p_interest_kind = 'unresolved' or p_price_basis = 'unresolved'
          or p_asset_scope = 'unresolved' then 'structure_unresolved'
        when p_interest_kind in ('partial_interest', 'debt_position', 'capital_solicitation',
                                 'business_only') then 'structure_not_property_interest'
        when p_price_basis = 'not_applicable'
         and ('auction' = any(coalesce(p_structure_tags, '{}'::text[]))
              or p_price_basis_method is distinct from 'no_signal') then 'price_not_stated'
        when p_price_basis not in ('property_price', 'not_applicable') then 'price_basis_not_property_price'
        when p_asset_scope = 'portfolio' then 'portfolio_geography'
        when p_interest_kind in ('leased_fee', 'leasehold')
          or p_asset_scope in ('unit_within_property', 'multiple_units') then 'interest_or_scope_not_comparable'
        when p_leg = 'cap_rate' and p_size_basis = 'land_area' then 'size_basis_incompatible'
        when p_leg = 'price_per_sf'
         and (p_size_basis not in ('building_area', 'portfolio_total_area', 'unit_area',
                                   'land_area', 'not_applicable')
              or (p_size_basis = 'not_applicable'
                  and p_size_basis_method is distinct from 'no_signal')) then 'size_basis_incompatible'
        else null::text
    end
$function$

;
