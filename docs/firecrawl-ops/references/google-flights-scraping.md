# Google Flights Scraping with Firecrawl

**Use case:** Automated flight deal discovery for Atlas (travel agent)
**Local endpoint:** `http://localhost:3002/v1`
**Tested:** 2026-03-09

---

## What Works

| URL Pattern | Status | Content Size | Notes |
|-------------|--------|--------------|-------|
| `/explore?q=flights+from+Tulsa` | ✅ Excellent | 25-30KB | Full destination list |
| `/explore?q=flights+from+Tulsa+to+Hawaii` | ✅ Excellent | 24KB | Region-specific search |
| `/explore?q=flights+from+Tulsa+to+Mexico` | ✅ Excellent | 30KB | All Mexico destinations |
| `/explore?q=flights+from+Tulsa+to+Europe` | ✅ Excellent | 28KB | 40+ European cities |
| `/flights?q=...` (simple) | ⚠️ Limited | 2-3KB | UI shell only |
| `/flights/search?tfs=...` | ⚠️ Limited | 16KB | Encoded params, minimal data |

**Key insight:** Use `explore` URLs, not `flights/search`. The explore pages return structured markdown with all deal data.

---

## Optimal Scrape Request

```bash
curl -s -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.google.com/travel/explore?q=flights+from+Tulsa+to+Hawaii&curr=USD&hl=en",
    "formats": ["markdown"],
    "waitFor": 8000,
    "onlyMainContent": false
  }'
```

**Parameters explained:**
- `waitFor: 8000` - Wait 8 seconds for dynamic flight data to load
- `onlyMainContent: false` - Include full results (not just header)
- `formats: ["markdown"]` - Clean structured text output

---

## Sample Output Structure

```markdown
1.  ### Honolulu
    
    Apr 9–15
    
    1 stop 13 hr 12 min
    
    $460
    
2.  ### Kauai
    
    Apr 27–May 5
    
    1 stop 11 hr 37 min
    
    $474
    
3.  ### Kailua-Kona
    
    Apr 13–20
    
    1 stop 11 hr 20 min
    
    $428
```

---

## URL Parameter Guide

| Parameter | Purpose | Example |
|-----------|---------|---------|
| `q=flights+from+Tulsa` | Origin city | Required |
| `+to+Hawaii` | Destination region | Optional |
| `curr=USD` | Currency | USD, EUR, etc. |
| `hl=en` | Language | en, es, fr, etc. |

**Regions that work well:**
- `Hawaii`, `Mexico`, `Europe`, `Caribbean`, `Asia`, `South+America`
- Specific cities: `Los+Angeles`, `New+York`, `London`, `Paris`

---

## Deal Thresholds (Atlas Configuration)

| Route | Alert Threshold | Action |
|-------|-----------------|--------|
| TUL → Hawaii | < $500 | Immediate alert |
| TUL → Europe | < $500 | Immediate alert |
| TUL → Caribbean/Mexico | < $400 | Immediate alert |
| TUL → Domestic | < $250 | Daily digest |

---

## Automation Workflow

### 1. Daily Scrape Script

See: `scripts/google_flights_scrape.py`

```bash
# Scrape multiple regions
python3 scripts/google_flights_scrape.py \
  --origin "Tulsa" \
  --regions "Hawaii,Mexico,Europe,Caribbean" \
  --output-dir deals/
```

### 2. Parse to CSV

See: `scripts/parse_flight_deals.py`

```bash
# Parse latest scrape into structured CSV
python3 scripts/parse_flight_deals.py \
  --input deals/raw/gflights-2026-03-09.json \
  --output deals/flights-2026-03-09.csv
```

### 3. Cron Setup

```yaml
# Add to OpenClaw cron for daily 8AM runs
cron:
  action: add
  job:
    name: "daily-flight-deals"
    schedule: { kind: "cron", expr: "0 8 * * *", tz: "America/Chicago" }
    payload: { 
      kind: "systemEvent", 
      text: "Run daily flight deal scrape: python3 ~/.openclaw/skills/firecrawl-ops/scripts/google_flights_scrape.py --origin Tulsa" 
    }
    sessionTarget: "isolated"
```

---

## Health Check

Before scraping, verify Firecrawl is running:

```bash
bash ~/.openclaw/skills/firecrawl-ops/scripts/firecrawl_healthcheck.sh
```

Expected output:
```
[1/4] docker compose ps  # All services up
[2/4] API root check     # {"message":"Firecrawl API"}
[3/4] scrape smoke test  # success: true
[4/4] done
```

---

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| Empty results | Page still loading | Increase `waitFor` to 8000-10000ms |
| No prices shown | `onlyMainContent: true` | Set to `false` |
| "Loading results" text | JavaScript not executed | Check Playwright service is running |
| 0KB content | Firecrawl down | Run healthcheck, restart compose |

---

## Sample Deals Found (Tested 2026-03-09)

**Hawaii:**
- Kailua-Kona: $428 (1 stop, Apr 13-20)
- Kahului: $448 (1 stop, Apr 9-15)
- Honolulu: $460 (1 stop, Apr 9-15)

**Mexico:**
- Cancún: $389 (Nonstop, Jul 2-9) 🔥
- Tijuana: $356 (1 stop, Mar 30-Apr 6)
- San José del Cabo: $425 (2 stops, Apr 13-21)

**Europe:**
- Dublin: $556 (2 stops, Aug 24-Sep 2) 🔥
- Reykjavik: $579 (2 stops, Apr 27-May 5)
- Athens: $591 (2 stops, Aug 20-29)

---

## Related Files

- `SKILL.md` - Main skill documentation
- `scripts/google_flights_scrape.py` - Multi-region scraper
- `scripts/parse_flight_deals.py` - Markdown to CSV parser
- `references/tools-capabilities.md` - Full endpoint reference

---

*Last tested: 2026-03-09 by Atlas*
