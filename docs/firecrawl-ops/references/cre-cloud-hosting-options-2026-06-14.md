# CRE Collector Cloud Hosting Options (where to run the pipeline)

Status as of 2026-06-14: research synthesis and recommendation. Not yet
actioned. This document evaluates where and how to run the CRE listing
collection pipeline, with a focus on moving it off the local Mac mini (currently
blocked by macOS Full Disk Access / TCC, see
`../../../scripts/firecrawl-ops/cre_collector/START_HERE.md` Known Limits). It is
a decision aid, not a migration order. Re-verify all third-party pricing and
platform limits before committing: cloud limits change often, and several
figures below are flagged as unverified.

Priorities this recommendation optimizes for, in order (set by Cayman on
2026-06-14): (1) fits cleanly into the existing Supabase + Vercel stack with the
fewest new vendors, (2) least maintenance / most managed, (3) lowest cost,
(4) reliability. Budget tolerance: a small always-on container or VM at roughly
$5-30/month is acceptable. Preference: leaning toward self-hosting the scraping
engine, open to a hosted API only if it clearly wins.

Related references: `playwright-stealth-cloudflare.md` (anti-bot / stealth
context), `cre-intelligence-system-design.md` (architecture),
`cre-monitor-subsystem.md` (the cheap-enumeration change-tracking layer),
`cre-access-matrix.md` (per-source accessibility).

---

## 1. TL;DR / Bottom line

1. **Fix the Mac mini first, this week, regardless of any cloud decision.** The
   only thing stopping scheduled runs today is a one-time macOS Full Disk Access
   grant to `/bin/bash`. That is a 5-minute, $0 fix that restores the proven
   launchd setup and keeps collection on a residential IP (the best anti-bot
   posture). It is the baseline every cloud option must beat.

2. **The hosted Firecrawl Cloud API (Fork B) is ruled out on cost.** At this
   volume it runs into thousands of dollars per month, roughly 100x the budget.
   High confidence. See section 4.

3. **"Fits the existing stack" cannot be fully honored for the heavy scraping
   half.** Neither Supabase nor Vercel can run a multi-hour, browser-heavy,
   Docker-based scraping stack. Verified, hard platform limits. So the existing
   stack can host the database (already does) and the light Python half and
   optionally the scheduler/trigger, but the heavy collector forces either a new
   vendor or staying on the Mac mini. See sections 3 and 5.

4. **If moving to cloud: self-host the Firecrawl stack in a container on a small
   always-on VPS (Hetzner / DigitalOcean) or Railway, or on Fly.io Cron Manager
   for a scale-to-zero design.** Keep the DB on Supabase and optionally trigger
   the light Python half from Supabase `pg_cron` + `pg_net`. This split is the
   design that best matches the stated priorities. See sections 6, 7, 8.

5. **The gating risk is anti-bot IP reputation, not platform choice.** Datacenter
   egress IPs are blocked harder than residential IPs. Test block rates against
   CBRE and Colliers from the candidate platform before trusting any migration.
   This is the largest unverified factor and the one most likely to change the
   answer. See section 3.2.

---

## 2. System footprint (what we are actually trying to host)

The pipeline has two halves with very different weight. This split drives the
entire recommendation.

### 2.1 Light half (trivially portable)

- `cre_ingest.py`, `cre_monitor.py`, `cre_gate.py`.
- Python standard library only. Zero pip dependencies.
- Shell out to `psql` (libpq) and connect to Supabase Postgres via a
  `POSTGRES_URL` connection string read from an env file at runtime.
- Can run almost anywhere: a tiny serverless function, a cron job, a container
  step, or co-located with the heavy half.

### 2.2 Heavy half (the real constraint)

- `collect.ts` (Node/TypeScript via `tsx`; deps `@mendable/firecrawl-js` +
  `cheerio`).
- It does NOT scrape broker sites directly. Every scrape is delegated to a
  Firecrawl API instance over `FIRECRAWL_API_URL` (default
  `http://localhost:3002`). That instance is the self-hosted Firecrawl Docker
  Compose stack: API server + headless Chromium (Playwright browser service) +
  Redis + RabbitMQ + a Postgres-backed queue (`nuq-postgres`).
- Several broker sources require anti-bot evasion via Firecrawl `proxy: stealth`
  (confirmed in `sources/cbre.ts` and `sources/colliers-main.ts`).
- The daily full run scrapes ~15 broker sites with per-listing detail-page
  rendering. It is long (tens of minutes to a few hours) and browser / CPU /
  memory bound.
- The monitor run (every ~3h) does cheap enumeration (no detail render) but
  still pages every source through Firecrawl. Lighter, still browser-backed.
- Scheduling today: macOS launchd tiers (every-3h monitor, daily 06:30 full,
  weekly reconcile).

The light half is a non-problem to host. The heavy half is the entire question.

---

## 3. The two findings that reframe the decision

### 3.1 "Fits the existing stack" is impossible for the heavy half

Verified, hard limits (section 5 has detail):

- **Supabase Edge Functions**: 150s (Free) / 400s (Paid) wall-clock cap, ~256MB
  memory, Deno runtime cannot run Docker or Chromium. Fatal for a multi-hour
  browser job on three independent grounds.
- **Vercel Functions**: cannot run the Docker / browser stack at all,
  irrespective of duration limits.

Therefore the existing Supabase + Vercel stack can host: the database (already
does), the light Python half (if reworked), and a scheduler/trigger. It cannot
host the heavy collector. Any cloud home for the collector is a new vendor, or
the Mac mini stays. This is the unavoidable core of the decision.

### 3.2 The gating risk: datacenter IP reputation (weight this heavily)

- Datacenter egress IP ranges (AWS, GCP, Cloudflare, Fly, etc.) are blocked more
  aggressively by commercial anti-bot systems than residential IPs.
- The Mac mini runs on a residential connection. That is quietly part of why
  collection works today.
- The system already uses `proxy: stealth` on its hardest sources (CBRE,
  Colliers). Moving to datacenter IPs can raise block rates even with stealth on.
- The research could NOT firmly verify the self-hosted Firecrawl proxy
  mechanics (whether self-host lacks the "Fire-engine" anti-bot component and
  must bring its own residential proxy via `PROXY_SERVER` / `PROXY_USERNAME` /
  `PROXY_PASSWORD`). Those claims came back unverified (votes were rate-limited).

Implication: any datacenter-hosted self-host deployment likely needs a
residential / managed proxy in front, which adds cost and can erode the
$5-30/month budget. Do not migrate without first testing block rates from the
candidate platform's IPs against CBRE and Colliers specifically. This is the
single most decision-relevant open question.

---

## 4. The architectural fork: self-host (A) vs hosted API (B)

### 4.1 Fork B (hosted Firecrawl Cloud API) is ruled out on cost (high confidence)

Pointing `FIRECRAWL_API_URL` at `api.firecrawl.dev` is a one-env-var change and
removes all infra work, but the economics do not survive this volume:

- Firecrawl bills 1 credit per page for Scrape/Crawl/Map, and 1 credit per page
  per check for Monitor.
- The largest self-serve tier (Scale) is 1,000,000 credits/month at $599/month.
- The daily full run alone is tens of thousands of detail renders. Even at a
  conservative estimate this exceeds 1,000,000 credits/month before any monitor
  traffic. A full-board estimate (~87,000 renders/day x 30) is ~2.6M base
  credits/month, about 2.6x the Scale tier.
- The `proxy: stealth` mode that CBRE and Colliers require costs 5 credits per
  page (a 5x multiplier on the dominant cost driver).
- The 8x/day monitor enumeration across ~15 sources stacks on top.
- Overage runs roughly $177 per 175,000 extra credits, so the daily run alone
  adds on the order of $1,600+/month in overage before the stealth multiplier.

Net: thousands of dollars per month, orders of magnitude past the budget. Note:
Firecrawl DOES offer pay-as-you-go overage (a claim that it does not was
refuted), but metering only makes the large number precise, it does not rescue
it. Decision: do not use Fork B for the bulk path. (It could still be a narrow
fallback for a handful of hardest-to-self-host sources, but that is a separate,
small-volume question.)

### 4.2 Fork A (self-host the Firecrawl stack in the cloud) is the path

No per-scrape fees. Full control. You operate the headless-browser + proxy
infra. This matches the stated lean. The cost is operational (running a
multi-container stack reliably) plus the anti-bot proxy question in 3.2. The rest
of this document is about where to run Fork A.

---

## 5. Platform-by-platform analysis

Confidence tags: [verified] = unanimous adversarial confirmation against primary
docs; [soft] = surfaced but not independently verified (treat as a lead).

### 5.1 Mac mini, fix Full Disk Access (the baseline to beat)

- **Benefits**: $0. The launchd tiers are already written and proven. Residential
  IP = best anti-bot posture. The current blocker is purely a one-time TCC / Full
  Disk Access grant to `/bin/bash` because the repo lives under `~/Documents`.
  Lowest effort by far.
- **Downsides**: home-network dependence, no managed uptime or failover,
  single-point-of-failure hardware. None of which the cheap cloud options fully
  solve without reintroducing datacenter-IP block risk.
- **Verdict**: do this first regardless. On anti-bot reliability and cost it is
  hard to beat. Confidence: the blocker and fix are codebase-grounded and
  certain; the reliability-vs-cloud tradeoff is a judgment call.

### 5.2 Supabase

- **Edge Functions**: 150s/400s wall-clock, ~256MB memory, no Docker/Chromium.
  [verified] Disqualified for the heavy collector.
- **Useful role**: keep the database here (already there). Use `pg_cron` +
  `pg_net` to TRIGGER the pipeline on a schedule, or to run the light Python half
  if reworked off `psql`. This is how the "fits existing stack" priority is
  partially honored.
- **Verdict**: scheduler/trigger and DB host, not compute host for scraping.

### 5.3 Vercel

- **Functions / Fluid Compute / Cron**: cannot run the Docker + browser stack.
  The exact max-duration figure was contested in research (a specific 800s claim
  was refuted), but duration is moot because the runtime cannot host the stack.
- **Useful role**: EQUIRE already runs here. Vercel Cron could trigger a job
  elsewhere, and the light half could run as a function if reworked.
- **Verdict**: trigger/orchestration or light half only, never the heavy collector.

### 5.4 Cloudflare

- **Browser Rendering**: Free tier caps browser time at 10 minutes/day with at
  most 3 concurrent browsers. Workers Paid includes 10 browser-hours/month then
  $0.09/additional browser-hour. [verified] A multi-hour daily render plus 8x/day
  enumeration exhausts this within days.
- **Containers / Sandboxes**: reached GA on 2026-04-13 on the Workers Paid plan
  ($5/month base) with Active-CPU pricing (pay for used CPU cycles, scales to
  zero). [verified] But memory and disk are billed on provisioned instance
  resources while the container is awake [soft, 2-1], so "pay only for used
  cycles" is true only for CPU and only while idle. A multi-hour run keeps the
  container awake and accruing memory/disk charges.
- **Verdict**: Browser Rendering API cannot absorb the workload. Containers are
  newly viable for self-hosting but the real all-in cost is understated by the
  Active-CPU headline. Possible, not optimal.

### 5.5 AWS

- **Lambda**: 15-minute hard execution cap. [verified, well-known] Fatal for the
  long collect run even with container images.
- **Fargate / ECS scheduled tasks (via EventBridge Scheduler)**: can run the
  Docker stack as a scheduled task with no execution-time problem. Heaviest ops
  and IAM surface of the options here.
- **AWS Batch / EC2 + cron**: also workable, more to manage.
- **Verdict**: Fargate/ECS is technically capable but the most operationally
  heavy, and a new vendor. Not the low-maintenance pick.

### 5.6 Google Cloud Run Jobs

- **Timeout**: default 10 minutes, configurable up to 168 hours (7 days) via
  `--task-timeout`. [verified] The long collect run is never constrained by
  execution time (unlike Lambda).
- **Model**: scales to zero between runs (pay only while running), triggerable on
  a cron schedule via Cloud Scheduler [soft], runs a container image with
  headless Chromium.
- **Caveat**: a single Job instance is capped at 8 vCPU / 32 GiB [soft].
  Generous for one headless-Chromium job, but running the full multi-container
  Firecrawl Compose stack inside one task likely needs a combined single-image
  build or splitting services. The multi-container packaging story is unconfirmed.
- **Verdict**: technically the strongest managed scale-to-zero fit for the heavy
  step, but it is a new vendor (GCP) and the packaging detail needs hands-on
  confirmation. Heavier onboarding than Railway / a VPS.

### 5.7 Fly.io Cron Manager

- **Model**: a small always-on Fly app watching a `schedules.json` file that
  boots a fresh ephemeral Machine per job (specified image, region, CPU/memory),
  runs the command, then tears it down. Full isolation between runs, scales to
  zero between jobs. [verified]
- **Fit**: maps cleanly onto replacing the three launchd tiers (every-3h
  monitor, daily full, weekly reconcile) with three schedule entries.
- **Caveat**: Cron Manager is a Fly community/blueprint tool, not a fully
  first-party managed feature, so slightly more operator responsibility.
- **Verdict**: the cleanest scale-to-zero, launchd-equivalent design. Strong
  option if you want per-job isolation and no always-on billing.

### 5.8 Railway

- **Model**: a one-click template reportedly deploys the full multi-service
  Firecrawl stack (API, worker, extract-worker, Playwright/headless-Chromium,
  Redis, nuq-Postgres) as a container set, reportedly ~$5-10/month depending on
  usage. [soft, unverified] Cron + services supported.
- **Verdict**: potentially the lowest-friction path to a self-hosted Firecrawl
  stack, but the template composition and cost are unverified. Confirm the
  template actually builds the whole stack and price a real run before relying on
  it.

### 5.9 Render / DigitalOcean / Hetzner (cheap always-on)

- **Render**: Cron Jobs + Background Workers + Docker support. Credible.
- **DigitalOcean**: App Platform or a Droplet running `docker compose` + cron.
- **Hetzner**: cheapest VPS, run `docker compose` + cron. ~$5-20/month.
- **Verdict**: a small VPS (Hetzner or DigitalOcean) running the existing
  `docker compose` stack plus cron is the simplest mental model: it is your local
  setup moved to a Linux box. Always-on (no scale-to-zero), but at $5-20/month
  that is immaterial. Datacenter-IP anti-bot risk applies (section 3.2).

---

## 6. Recommended split architecture

This is the design that best satisfies the priority order (existing-stack fit >
managed > cost > reliability), given that the heavy half cannot live on
Supabase/Vercel:

```
                 schedule (pg_cron+pg_net on Supabase, or platform cron)
                                    |
                                    v
  [ HEAVY half: container ]   collect.ts  -> Firecrawl Docker stack (Chromium)
   on VPS / Railway / Fly /        |          + residential proxy (see 3.2)
   Cloud Run Job                   v
                              artifact JSON
                                    |
                                    v
  [ LIGHT half ]            cre_ingest.py / cre_monitor.py / cre_gate.py
   same container, or             |   (Python stdlib + psql)
   Supabase-triggered             v
                          Supabase Postgres  ->  EQUIRE (Vercel)  [unchanged]
```

- **Database**: stays on Supabase (already there). No change.
- **Heavy collect**: self-hosted Firecrawl stack in a container on the chosen
  box, with a residential proxy in front if the anti-bot test (3.2) requires it.
- **Light ingest/monitor/gate**: simplest is to run them in the same container
  right after `collect.ts` finishes (one job, one box). The more
  "fits-the-stack" variant runs them serverless and triggers from Supabase
  `pg_cron` + `pg_net`, but that adds a handoff for marginal benefit. Co-locating
  is the lower-maintenance default.
- **Scheduling**: replace the launchd tiers with the platform's cron (Cloud
  Scheduler, Fly Cron Manager schedules, Railway/Render cron, or plain crontab on
  a VPS).

---

## 7. Containerization plan (portability across all of the above)

Containerizing makes the pipeline portable so the platform choice is reversible
and not a lock-in.

- **Firecrawl stack**: reuse the upstream `docker compose` (API + playwright
  service + redis + rabbitmq + nuq-postgres). On a VPS this runs as-is. On
  single-task platforms (Cloud Run Job, Fly Machine) it likely needs either a
  combined image or running the stack as a long-lived service that the job talks
  to over the network. This is the main packaging unknown to resolve per platform.
- **Job container**: a small image that runs `collect.ts` (Node + tsx) then the
  three Python scripts (stdlib + `psql`). Multi-stage build: a Node stage for the
  collector, a thin Python+libpq stage for ingest. Entry point script chains
  collect -> ingest -> monitor -> gate with the same flags the launchd tiers use
  (`--no-mark-missing`, status activation OFF by default, etc.).
- **Secrets**: `POSTGRES_URL` / `POSTGRES_URL_NON_POOLING`, `FIRECRAWL_API_KEY`
  (any non-empty value for self-hosted with `USE_DB_AUTHENTICATION=false`),
  `FIRECRAWL_API_URL`, and any `PROXY_SERVER` / `PROXY_USERNAME` /
  `PROXY_PASSWORD`. Use the platform secret store, never bake into the image,
  never commit. The ingestor reads the URL from an env file at runtime and prints
  only the path, never the URL.
- **Compose-as-a-single-task**: docker-compose-style multi-service stacks do not
  run natively as one task on most serverless-container platforms without either
  Kubernetes-style orchestration or a single combined image. A plain VPS sidesteps
  this entirely (compose runs as designed). This is a point in favor of the VPS
  for simplicity.

---

## 8. Recommendation and phased plan

### Phase 0 (do now, this week, $0)

Grant macOS Full Disk Access to `/bin/bash` and unblock the launchd tiers. This
solves the actual current problem (nothing is running), keeps collection on the
residential IP, and creates breathing room to evaluate cloud without pressure.
See `START_HERE.md` Known Limits for the exact steps. Verify a kickstarted run
exits 0 and writes only the expected tables.

### Phase 1 (only if moving off the Mac is still desired)

1. Containerize the pipeline (section 7) so the choice is portable.
2. Run the anti-bot test (section 3.2): stand up the stack on the candidate
   platform, scrape CBRE + Colliers with stealth, compare block rates to the Mac.
   - If clean: proceed.
   - If blocked: add a residential proxy and re-price against the budget, or keep
     collection on the Mac.
3. Pick the box by appetite:
   - **Lowest friction, simplest model**: a small Hetzner / DigitalOcean VPS
     running the existing `docker compose` + cron, or Railway if its one-click
     template checks out.
   - **Scale-to-zero, cleanest launchd replacement**: Fly.io Cron Manager.
   - **Fully managed scale-to-zero, willing to onboard GCP**: Cloud Run Jobs
     (resolve the multi-container packaging first).
4. Keep the DB on Supabase. Optionally trigger the light half from Supabase
   `pg_cron` + `pg_net`; otherwise co-locate it in the job container.

### What NOT to do

- Do not adopt the hosted Firecrawl Cloud API for the bulk path (section 4.1).
- Do not put the heavy collector on Supabase Edge Functions or Vercel Functions
  (section 3.1).
- Do not migrate to a datacenter IP without the anti-bot test (section 3.2).

---

## 9. Open questions to resolve before migrating

1. Do CBRE / Colliers / JLL anti-bot systems materially raise block rates against
   datacenter egress IPs versus the Mac mini's residential IP, even with stealth
   on? (Most decision-relevant. Test empirically.)
2. Can self-hosted Firecrawl match the anti-bot capability the workload depends
   on, or must it be paired with a paid residential proxy (Bright Data, Oxylabs,
   IPRoyal, etc.) via `PROXY_SERVER`? What does that proxy cost at this scrape
   volume, and does it dominate the cloud bill?
3. What is the real all-in monthly cost of the full multi-container stack as
   scheduled Cloud Run Jobs or Fly Machines once memory/disk and per-run CPU are
   summed, and does it stay within $5-30/month or balloon with a residential
   proxy?
4. Is the Supabase-triggered split (light half serverless) worth the handoff
   complexity versus co-locating ingest in the job container?

---

## 10. Confidence and evidence notes

- **Well verified** (unanimous against primary docs): Firecrawl cost kill (4.1),
  Supabase Edge Function limits (5.2), Vercel cannot host the stack (5.3),
  Cloudflare Browser Rendering tiers and Containers GA + Active-CPU (5.4), Lambda
  15-minute cap (5.5), Cloud Run Jobs 7-day timeout (5.6), Fly Cron Manager model
  (5.7).
- **Unverified / soft** (surfaced but not independently confirmed; rate-limited
  verification votes): Railway one-click template composition and ~$5-10/month
  (5.8), Cloud Run multi-container packaging and 8 vCPU/32 GiB ceiling and Cloud
  Scheduler trigger mechanism (5.6), the self-hosted Firecrawl proxy mechanics
  and the datacenter-IP block-rate differential (3.2). Treat these as leads, not
  facts.
- **Time-sensitivity**: all pricing and limits are 2025/2026-current as of the
  research window (Firecrawl pricing May-June 2026; Cloudflare Browser Rendering
  billing started 2025-08-20; Cloudflare Containers GA 2026-04-13). Cloud pricing
  changes frequently. Re-verify before committing.
- **Workload figures**: the ~87,000-listing and tens-of-minutes-to-hours runtime
  figures are from the project brief and `CLAUDE.md`, not independently
  re-measured. The cost conclusion in 4.1 is robust to a wide range of the actual
  daily render count.

---

## 11. Sources

Primary:
- https://www.firecrawl.dev/pricing
- https://docs.firecrawl.dev/features/stealth-mode
- https://github.com/firecrawl/firecrawl/blob/main/SELF_HOST.md
- https://supabase.com/docs/guides/functions/limits
- https://docs.cloud.google.com/run/docs/configuring/task-timeout
- https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule
- https://developers.cloudflare.com/browser-rendering/platform/pricing/
- https://developers.cloudflare.com/changelog/post/2026-04-13-containers-sandbox-ga/
- https://developers.cloudflare.com/containers/pricing/
- https://fly.io/docs/blueprints/task-scheduling/
- https://github.com/fly-apps/cron-manager
- https://vercel.com/changelog/higher-defaults-and-limits-for-vercel-functions-running-fluid-compute

Secondary / blog (corroborating, treat with care):
- https://www.eesel.ai/blog/firecrawl-pricing
- https://railway.com/deploy/firecrawl
- https://sliplane.io/blog/5-cheap-ways-to-deploy-docker-containers-in-2025
- https://stevescargall.com/blog/2026/04/self-hosting-firecrawl-on-ubuntu-25.04-with-docker-compose/
- https://northflank.com/blog/railway-vs-render
- https://betterstack.com/community/guides/web-servers/digitalocean-vs-hetzner/
- https://torchproxies.com/datacenter-vs-residential-proxies-2026/
- https://scrapfly.io/blog/posts/how-to-bypass-cloudflare-anti-scraping
- https://nunn.au/2023/11/28/tcc-launchd-woes
