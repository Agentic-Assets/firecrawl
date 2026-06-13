# Avison Young Detail Enrichment Proof - 2026-06-12

Scope: `avison-young` only. Public URL-only investigation. No binary PDF or
image downloads, no auth, no consent or gated claims, no live Supabase ingest.

## Question

Does the current SharpLaunch public feed already capture all defensible public
fields, or can the collector be enriched with public detail URLs, broker
profile or contact URLs, document URLs, and richer image URLs?

## Commands Run

Repository and docs:

```bash
git status --short --branch
sed -n '1,180p' scripts/firecrawl-ops/cre_scrapers/brokers/avison_young/README.md
sed -n '1,130p' scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md
sed -n '1,120p' scripts/firecrawl-ops/cre_collector/START_HERE.md
sed -n '1,180p' docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md
sed -n '2560,2790p' scripts/firecrawl-ops/cre_collector/collect.ts
```

Targeted public probes:

```bash
node --input-type=module <<'NODE'
# Inline direct-fetch probe:
# 1. Fetch Avison Young property page and parse the public SharpLaunch key.
# 2. Fetch public SharpLaunch website and team_member entities.
# 3. Probe four representative public detail URLs on both avisonyoung.us and
#    the corresponding SharpLaunch microsite.
# 4. Count public PDF URLs, image URLs, JSON-LD, broker profile links, and
#    VCard-like links. Write JSON proof to /tmp.
NODE
```

Collector and dry-run ingest baseline:

```bash
cd scripts/firecrawl-ops/cre_collector
npx tsx collect.ts --source=avison-young --transaction=both --max-items=2 --concurrency=2 --out=/tmp/avison_young_current_collector_probe.json
python3 cre_ingest.py --in /tmp/avison_young_current_collector_probe.json --dry-run --keep-artifacts /tmp/avison_young_current_collector_ingest_check
npm run typecheck
```

## Artifacts

- Detail probe JSON: `/tmp/avison_young_detail_probe_2026-06-12T2028.json`
- Current collector baseline: `/tmp/avison_young_current_collector_probe.json`
- Dry-run ingest SQL: `/tmp/avison_young_current_collector_ingest_check/ingest.sql`

## Findings

The SharpLaunch feed is still the right public discovery spine. At probe time,
the public `website` entity returned 2,201 active rows, 2,199 US-compatible
rows, 769 sale-like rows, and 1,563 lease or sublease rows. The only
URL-bearing listing fields in the feed were `external_url`, `url`,
`image_path`, and `team_member_ids`. The public `team_member` entity returned
857 rows with email, phone or secondary phone, and `media_id` where available.

The existing collector captures the defensible feed-level fields: stable
SharpLaunch id, Avison detail URL, SharpLaunch microsite URL, one feed image
URL, joined team-member contacts, email, phone, title, company/location, and
avatar CDN URL. The dry-run baseline staged 4 probe listings with 4 image rows,
5 contact rows, 0 document rows, 0 profile URLs, and 0 VCard URLs.

Public detail pages can enrich beyond the current collector. Four representative
rows were probed:

| Listing id | Listing | Public PDFs | Extra SharpLaunch property images | JSON-LD on Avison page | Broker profile URLs | VCard URLs |
|---|---|---:|---:|---:|---:|---:|
| 17341 | 4316 J M Turk Road | 3 | 11 | 1 | 0 | 0 |
| 17353 | Kings Plaza Land | 1 | 2 | 1 | 1 | 0 |
| 17373 | 18 Acres on Davenport Rd | 1 | 15 | 1 | 0 | 0 |
| 17304 | Alma School Corporate Center III | 1 | 7 | 1 | 0 | 0 |

Net gain in the small sample: 6 unique public PDF URLs and at least 35
property-specific SharpLaunch image URLs, compared with 0 document URLs and 4
feed image URLs from the current collector. The Avison-hosted pages also expose
JSON-LD listing data in these samples. The SharpLaunch microsites are cleaner
for PDF and gallery extraction because they avoid much of the generic Avison
navigation media.

Broker profile URL coverage is real but sparse in this sample. One listing
exposed a broker-specific public profile URL:
`https://www.avisonyoung.us/web/phoenix/professionals/-/ayp/view/matt-schrauth/in/phoenix`.
Other profile-like hits were generic navigation links such as company profile,
media contacts, office contacts, and research contacts, so those should not be
stored as broker profile URLs. No VCard or `.vcf` URL was found in the sample.

## Limits

- Feed counts drifted slightly from earlier notes, likely because the active
  public feed changed between runs. This pass observed 2,199 US-compatible rows,
  not the prior 2,200.
- Detail enrichment is public but not cheap at full-feed scale. Enriching all
  2,199 current US-compatible rows would add thousands of page requests if both
  Avison and SharpLaunch URLs are fetched.
- The probe did not download PDFs or images. It only counted and recorded public
  URLs present in HTML.
- No gated document room, consent wall, auth-only endpoint, or private contact
  path was used or claimed.
- VCard URLs remain unproven for Avison Young.

## Safe Next Steps

1. Keep the default Avison Young collector on the public SharpLaunch feed so
   full refreshes stay fast and low-risk.
2. Add an opt-in, bounded detail-enrichment mode for Avison Young only, for
   example an environment flag or source-specific debug flag, before enabling
   any full-feed detail sweep.
3. In that detail mode, fetch the SharpLaunch microsite first for PDFs and
   clean galleries, then fetch the Avison `external_url` only when JSON-LD or
   broker profile links are needed.
4. Store only `brochures` and `photos` URL arrays plus broker `profileUrl` when
   a broker-specific `/professionals/-/ayp/view/` link is visible. Do not store
   generic Avison navigation contact links as broker profiles.
5. Run another dry-run ingest on a bounded sample after any collector patch and
   confirm document and image child rows contain URLs only.

Conclusion: the current collector captures the public SharpLaunch feed well, but
it does not capture all defensible public fields. Public detail pages can add
PDF document URLs, richer property image URLs, JSON-LD facts, and occasional
broker profile URLs. The right implementation path is opt-in bounded enrichment,
not changing the default full-feed run in one step.
