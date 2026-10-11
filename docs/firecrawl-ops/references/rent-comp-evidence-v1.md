# Collector rent evidence v1

`cre_ingest.to_row` writes `raw_data.rent_comp_evidence_v1` after existing source
privacy redaction. Dual sale/lease merges retain each observation under
`primary` / `secondary_pass`; consumers must not combine contradictory generations.
This is source evidence, never market qualification or an OM-facts writer.
GetCREdata owns qualification and OM extraction.

The envelope contains `version: 1`, `rent`, `areas`, `coordinates`,
`source_updated_at`, and `collected_at`. Clocks use the ingest source lastmod and
actual observation timestamp respectively, ISO strings or null. Missing clocks
must not be replaced with the current time.

`rent` contains `raw_quote`, `source_field_label`, `original_period`
(`annual`, `monthly`, `unknown`, `conflict`), `denominator` (`sf`, `unknown`, `conflict`),
`currency` (explicit ISO code, `unknown`, `conflict`), `lease_basis`,
`annual_psf_min`, `annual_psf_max`, and `anomalies` (string array).
The raw quote is `leaseRateText`; the optional original label is
`leaseRateSourceLabel`. Missing source labels remain null. A generic adapter
field name is not an original provider label. Bare dollars do not establish USD.
Only explicit USD, SF, and one period can produce annual USD/SF values.
Monthly rates multiply by 12. Conflicting periods or currencies remain unresolved.
Numbers are associated with a currency marker or the start of a structured rate
field, so lease-term years cannot become rent. Leading decimal quotes are supported.
Positive amounts round to cents using IEEE-754 scaling and half-up rounding in
both implementations. Conflicting NNN/gross bases set basis null and add
`lease_basis_conflict`. Area ranges keep their original quote and a null scalar;
an upper bound is never substituted for the offered-space area.
High values and wide ranges retain both bounds and receive review flags;
there is no magnitude rejection, clamp, or pre-parsed scalar bypass.

Source-field audit against committed fixtures: Marcus `Rent Per Square Feet`
with `$23.40` does not supply period or ISO currency; Cushman `4.50/SF USD`
lacks period and `$30 (Annual) USD` lacks denominator. Transwestern availability
bare dollar rates remain unresolved. AY dollar-only annual quotes do not prove
ISO currency at any magnitude. NAI's historical POUND-to-USD rent reinterpretation
is no longer admitted by the backfill. Explicit `3.59 USD/SF/MO` remains supported.
These are fixture observations, not a claim about current live source inventory.

Each `areas` item contains `raw_quote`, `source_field_label`, `area_kind`
(`building`, `offered_space`, `land`, `units`, `unknown`), `area_unit`
(`sf`, `acres`, `units`, `unknown`), and `value`. Explicitly named source text
fields establish the area kind. Generic `sizeText` stays unknown even with SF.
Units and acres stay in their original units. Consumers must reject conflicting
area observations and must not infer building area from land, offered space,
unit counts, or normalized `sizeSf` alone.

Coordinates contain `latitude`, `longitude`, `precision`, `source_field_label`,
and `raw_quote`. Current generic listing coordinates always have precision
`unknown` and null precision provenance. Future audited adapters may emit
`rooftop` or `parcel` only with source evidence; decimal precision, ZIP centroids,
and `geo_source` are insufficient.

Offline replay: call the pure `replay_evidence` function on saved, explicitly
selected `{listing, source_updated_at, collected_at}` rows (maximum 1,000 per review batch), passing original source
and observation clocks. Compare resulting envelopes without database writes.
Do not run collectors, existing backfill apply modes, or production repair as
part of this change. Historical rows without evidence remain unqualified until
the source-owning refresh workflow provides it.
