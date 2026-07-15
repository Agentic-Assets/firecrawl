# brokers/ Module

## Most Critical Rule

**STALE for production.** Nothing under this folder runs on the daily schedule or
writes the live EQUIRE board. **`../../cre_collector/sources/*.ts`** is the only
active per-broker code. Edit `scraper.py` here only for manual experiments or
archived probe notes.

## ACTIVE vs STALE (agent map)

| Legacy folder (`cre_scrapers/brokers/`) | Status | Active production code |
|----------------------------------------|--------|-------------------------|
| `cbre/` | STALE `scraper.py` | `cre_collector/sources/cbre.ts` (+ `cbre-dealflow.ts`) |
| `jll/` | STALE `scraper.py` | `cre_collector/sources/jll.ts` (+ `jll-investor.ts`) |
| `cushman/` | STALE `scraper.py` | `cre_collector/sources/cushman-wakefield.ts` |
| `colliers/` | STALE `scraper.py` | `cre_collector/sources/colliers.ts` (+ `colliers-main.ts`) |
| `marcus_millichap/` | STALE `scraper.py` | `cre_collector/sources/marcus-millichap.ts` |
| `avison_young/` | STALE `scraper.py` | `cre_collector/sources/avison-young.ts` |
| `svn/` | STALE `scraper.py` | `cre_collector/sources/buildout.ts` (svn plugin) |
| `nai_global/` | STALE `scraper.py` | `cre_collector/sources/nai-global.ts` |
| `newmark/` | STALE `scraper.py` | `cre_collector/sources/newmark.ts` |
| `lee_associates/` | notes only | `cre_collector/sources/buildout.ts` (lee plugin) |
| `savills/` | notes only | `cre_collector/sources/savills.ts` |
| `transwestern/` | notes only | `cre_collector/sources/transwestern.ts` |
| *(no folder here)* | — | `matthews.ts`, `franklin-street.ts`, `srs.ts`, `hanley.ts`, `kidder-mathews.ts` |

**REFERENCE (safe to read):** `README.md`, `archive/`, dated `*.md` probe files.

## Folder-Specific Commands (legacy manual only)

```bash
cd scripts/firecrawl-ops
python3 -m compileall -q cre_scrapers/brokers
python3 -c "from cre_scrapers.jll import JLLScraper; JLLScraper().run(max_listings=3)"
```

Production probe instead: `cd cre_collector && npx tsx collect.ts --source=jll --max-items=6 --out=/tmp/probe.json`

## Naming Patterns

- Dir: underscores (`avison_young`); `SLUG`: hyphens (`avison-young`).
- `archive/`: probe artifacts only, not runtime.

## References

- **Active:** `../../cre_collector/CLAUDE.md`, `../../cre_collector/sources/CLAUDE.md`, `../../cre_collector/START_HERE.md`
- **Legacy parent:** `../CLAUDE.md`
- Per-broker: `README.md` in each subfolder
