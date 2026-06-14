Historical probe artifact (pre-2026-06-13). Production path: cre_collector/sources/.

# Newmark Safe Refinement Findings - 2026-06-12

Timestamp: 2026-06-12 08:02 CDT.

Scope: Newmark only, source key `newmark`. This pass was read-only against the
collector and public pages. No live ingest was run. No binaries were downloaded.
No collector code was changed because `cre_collector/collect.ts` already has
unrelated worktree edits.

## Commands Run

Context and docs:

```bash
sed -n '1,240p' scripts/firecrawl-ops/cre_collector/START_HERE.md
sed -n '1,240p' CLAUDE.md
sed -n '1,260p' scripts/firecrawl-ops/CLAUDE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/CLAUDE.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/HANDOFF_LOG_2026-06-11.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/LESSONS_2026-06-11.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/VALIDATION_2026-06-12.md
sed -n '1,260p' scripts/firecrawl-ops/cre_collector/BROKERAGE_STATUS_2026-06-12.md
sed -n '1,260p' docs/firecrawl-ops/references/cre-brokerage-completion-playbook.md
sed -n '1,260p' scripts/firecrawl-ops/cre_scrapers/brokers/newmark/README.md
sed -n '1,260p' scripts/firecrawl-ops/cre_scrapers/brokers/newmark/PERFORMANCE_ACCURACY_REVIEW_2026-06-12.md
```

Implementation and artifact checks:

```bash
git status --short
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector && npm run typecheck
sed -n '705,845p' scripts/firecrawl-ops/cre_collector/collect.ts
git diff -- scripts/firecrawl-ops/cre_collector/collect.ts | sed -n '1,260p'
python3 - <<'PY'
import json, pathlib
p = pathlib.Path('scripts/firecrawl-ops/cre_collector/out/newmark_full_2026-06-12_no_state_recovery.json')
j = json.loads(p.read_text())
rows = [x for x in j.get('listings', []) if x.get('sourceKey') == 'newmark' or x.get('company') == 'Newmark']
print('rows', len(rows), 'artifact_total', j.get('totalListings'))
print('null_state', sum(1 for x in rows if not x.get('state')))
print('with_contacts', sum(1 for x in rows if x.get('contactsDetailed')))
print('with_brokerIds', sum(1 for x in rows if x.get('brokerIds')))
print('with_docs', sum(1 for x in rows if x.get('brochures')))
print('with_photos', sum(1 for x in rows if x.get('photos')))
for x in rows:
    if not x.get('state'):
        print({k: x.get(k) for k in ['id', 'name', 'city', 'state', 'postalCode', 'url']})
PY
```

Small public probes:

```bash
node - <<'NODE'
const res = await fetch('https://www.nmrk.com/properties', { headers: { 'user-agent': 'Mozilla/5.0' } });
console.log(res.status, res.url, res.headers.get('content-type'));
console.log((await res.text()).slice(0, 80));
NODE
```

A larger direct Node probe attempted to read public Algolia config from
`https://www.nmrk.com/properties`, but direct fetch returned Cloudflare `403`
with `Your request was blocked.` The web-rendered public page and sample detail
page were then inspected only as text. The properties page and
`/properties/701-8th-st-nw-washington-lease` currently expose mostly the site
shell or investor portal prompt, not listing contacts, documents, galleries, or
VCard URLs. The public people profile
`https://www.nmrk.com/people/andrew-visnick` remains a valid profile page.

## Current Findings

- The collector already includes Newmark no-state recovery after state-facet
  splitting, but the row mapper still sets `state: clean(h.state)`. Recovered
  no-state rows therefore remain null-state rows in the artifact and database.
- The saved full artifact `out/newmark_full_2026-06-12_no_state_recovery.json`
  has 4,371 Newmark rows: 1,121 sale and 3,250 lease.
- Artifact field coverage from the local read:
  - 3 rows with null `state`.
  - 0 rows with `contactsDetailed`.
  - 0 rows with `brokerIds`.
  - 0 rows with document or brochure URLs.
  - 4,303 rows with image URLs, all from Algolia thumbnails.
- The three null-state rows are Washington, DC lease listings:
  - `701-8th-st-nw-washington-lease`, ZIP `20001`.
  - `800-maine-avenue-southwest-washington-lease`, ZIP `20024`.
  - `1800-massachusetts-avenue-northwest-washington-lease`, ZIP `20036`.
- The latest-batch Newmark Supabase QA independently reported the same shape:
  4,371 rows, 3 missing states, 0 contacts, 0 documents, and 4,303 image rows.
- Direct public `fetch()` from this host is currently blocked by Cloudflare for
  `https://www.nmrk.com/properties`. The local Firecrawl stack was not available
  because OrbStack's Docker socket was missing, so no fresh collector probe was
  run in this pass.
- `npm run typecheck` in `cre_collector` passed.
- `git status --short` showed `scripts/firecrawl-ops/cre_collector/collect.ts`
  already modified. The observed diff is Marcus & Millichap detail-cache work,
  unrelated to Newmark.

## Safe Refinement Plan

Patch only the Newmark block in `cre_collector/collect.ts` after coordinating
with the current `collect.ts` owner.

1. Add a Newmark-local state normalizer:
   - Prefer `h.state` when present.
   - Use `h.state_code` if present and two letters.
   - Infer `DC` only when `city` is `Washington` and ZIP starts with `200`.
   - Leave all other no-state rows null.
2. Preserve Newmark broker provenance on each listing object so it lands in
   `raw_data`: `broker_name`, `broker_id`, `broker_ids`, `second_broker_id`,
   and `third_broker_id`.
3. Add a cached public People Algolia lookup keyed by normalized
   `broker_name`.
   - Query `sectionGroup:People`, `siteHandle:enUs`, `query=<broker_name>`.
   - Accept only exact case-insensitive name/title matches.
   - Populate `contactsDetailed` with name, email, phone, company, title or
     office fields where present, and `profileUrl`.
   - Set `brokerIds` from deterministic profile or name keys if the ingestor
     needs stable child contact ordering. Do not use numeric Buildout IDs as
     People joins unless a real join is proven.
4. Keep detail-page scraping out of the bulk Newmark path for now. The safe
   evidence still says detail pages are noisy shells or investor-portal prompts.
5. Keep documents empty unless a listing-specific public document URL is found.
   Do not store Newmark site-wide legal PDFs as listing documents.
6. Keep images limited to Algolia thumbnails until a reliable listing-gallery
   source is proven. Prefer the largest thumbnail URL already exposed by the
   feed.
7. Keep VCard URLs empty unless a public profile or People endpoint exposes a
   real VCard or contact-card URL.

## Verification Path For The Patch

Use no live ingest first:

```bash
cd /Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
cd scripts/firecrawl-ops/cre_collector
npm run typecheck
npx tsx collect.ts --source=newmark --transaction=both --max-items=30 --page-cap=5 --concurrency=2 --out=/tmp/newmark_refinement_probe_2026-06-12.json
python3 cre_ingest.py --in /tmp/newmark_refinement_probe_2026-06-12.json --dry-run --keep-artifacts /tmp/newmark_refinement_ingest_check_2026-06-12
```

Expected small-probe checks:

- The three known Washington, DC rows map to state `DC` when included.
- Newmark rows with exact public People matches have `contactsDetailed`.
- Newmark document rows remain 0 unless a listing-specific URL is proven.
- Image rows remain URL-only Algolia thumbnails.
- Dry-run ingest stages rows without missing URLs or child-orphan warnings.

Then run a Newmark-only full dry run before any live ingest decision:

```bash
npx tsx collect.ts --source=newmark --transaction=both --max-items=0 --page-cap=400 --concurrency=2 --out=out/newmark_full_refined_2026-06-12.json
python3 cre_ingest.py --in out/newmark_full_refined_2026-06-12.json --dry-run --keep-artifacts /tmp/newmark_full_refined_ingest_check_2026-06-12
```

Live ingest should remain a separate explicit decision.
