# Firecrawl Setup Progress Summary - January 6, 2026

## Work Completed

### 1. Environment Configuration
- **AI Provider**: Configured Firecrawl to use **OpenRouter** (`MODEL_NAME=xiaomi/mimo-v2-flash:free`).
- **Database**: Switched to **Supabase** as the persistent backend (`USE_DB_AUTHENTICATION=true`).
- **Stability**: Set `DISABLE_BLOCKLIST=true` and `DISABLE_ENGPICKER=true` to prevent API crashes caused by missing advanced cloud-only tables.
- **Docker**: Optimized `docker-compose.yaml` to pass all Supabase environment variables (`SUPABASE_URL`, `SUPABASE_SERVICE_TOKEN`, etc.) to the containers.

### 2. Database Integration (Shared Project Safety)
- **Safety Zone**: Established a naming convention where Firecrawl only interacts with **lowercase** tables (`scrapes`, `crawls`, `api_keys`). This ensures zero conflict with the other application's **CamelCase** tables (`Chat`, `User`, `Document`).
- **Initialization**: 
    - Initialized the `nuq` schema for job queuing.
    - Created the `public` schema tables required for logging and authentication.
- **Self-Hosted Mocks**: Created a full set of mock RPCs (Stored Procedures) in Supabase to bypass cloud billing logic while remaining compatible with the "production" auth path.
- **Persistent Storage Fix**: Successfully dropped and recreated the Firecrawl tables to add a `content` column (for markdown/HTML) and a `result` column (for AI extractions). This resolved "upstream timeout" issues caused by locks from other applications.

### 3. API Logic Updates
- **Content Saving**: Modified `apps/api/src/services/logging/log_job.ts` to actively save the full markdown/HTML output into the Supabase `scrapes` table.
- **Extraction Saving**: Updated the `/v1/extract` workflow to save the final AI JSON result directly into the Supabase `extracts` table.
- **SQL Template**: Updated `apps/nuq-postgres/supabase-setup.sql` to include these persistent storage columns for any future deployments.

### 4. Functional Verification
- **Scrape Test**: Confirmed that `Invoke-RestMethod` to `/v1/scrape` successfully renders markdown and saves it to the `content` column in Supabase.
- **Extract Test**: Verified that the `/v1/extract` endpoint processes prompts correctly and logs the activity to the `extracts` table.

### 5. Access Information
- **Local API**: `http://localhost:3002`
- **Authenticated Token**: `fc-31dba252482749989356775a972cd48a`
- **Supabase Project ID**: `fhqycqubkkrdgzswccwd`

## 🛡️ Important Rules & Safety
- **DO NOT** modify any tables starting with Capital Letters.
- **DO NOT** run destructive SQL (DROP/TRUNCATE) on non-Firecrawl tables.
- **ALWAYS** ensure new logging columns (`content`, `result`) are included in database schema updates.
