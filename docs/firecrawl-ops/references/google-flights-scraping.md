# Google Flights Scraping With Local Firecrawl

Use this as a workflow note, not a promise that Google Flights will always expose the same markup. Travel pages change often; refresh with the scripts before relying on old results.

## Current Local Shape

- Local API: `http://localhost:3002/v2`
- Script: `scripts/firecrawl-ops/google_flights_scrape.py`
- Parser: `scripts/firecrawl-ops/parse_flight_deals.py`
- Best target pattern observed: `https://www.google.com/travel/explore?...`
- Less useful target pattern: deep `/flights/search?...` pages can return mostly UI shell

## Scrape Request

```bash
curl -sS -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.google.com/travel/explore?q=flights+from+Tulsa+to+Hawaii&curr=USD&hl=en",
    "formats": ["markdown"],
    "waitFor": 8000,
    "onlyMainContent": false
  }'
```

Notes:
- Increase `waitFor` if the markdown still says results are loading.
- Keep `onlyMainContent:false` when the useful flight list is outside the detected main article region.
- Treat parsed prices/dates as a lead-generation signal and verify before booking.

## Script Workflow

```bash
python3 scripts/firecrawl-ops/google_flights_scrape.py \
  --origin "Tulsa" \
  --regions "Hawaii,Mexico,Europe,Caribbean" \
  --output-dir deals/ \
  --save-raw
```

Parse saved raw output to structured CSV/JSON:

```bash
python3 scripts/firecrawl-ops/parse_flight_deals.py \
  --input deals/raw/gflights-YYYY-MM-DD-hawaii.json \
  --output deals/flights-YYYY-MM-DD.csv
```

## Health Check

```bash
bash scripts/firecrawl-ops/firecrawl_healthcheck.sh
```

From another repo after skill sync:

```bash
~/.agents/skills/firecrawl-ops/scripts/firecrawl_healthcheck.sh
```

## Useful Regions

- `Hawaii`
- `Mexico`
- `Europe`
- `Caribbean`
- `Asia`
- `South America`
- specific cities such as `Los Angeles`, `New York`, `London`, or `Paris`

## Troubleshooting

| Issue | Likely cause | Fix |
|---|---|---|
| Empty results | Page still loading | Increase `waitFor` to 8000-10000ms |
| No prices shown | Content outside main region | Set `onlyMainContent:false` |
| UI shell only | Wrong URL family | Prefer `/travel/explore` URLs |
| Request fails | Local stack down | Run healthcheck, then inspect `docker compose logs api --tail 200` |

## Related Files

- `scripts/firecrawl-ops/google_flights_scrape.py`
- `scripts/firecrawl-ops/parse_flight_deals.py`
- `docs/firecrawl-ops/references/tools-capabilities.md`
