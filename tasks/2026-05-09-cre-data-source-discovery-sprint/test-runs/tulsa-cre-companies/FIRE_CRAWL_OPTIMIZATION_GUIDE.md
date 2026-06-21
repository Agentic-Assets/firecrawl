# Firecrawl Optimization & Guidance Guide

This document summarizes the learnings and best practices discovered during the Tulsa Commercial Real Estate (CRE) "Test Run".

## 🚀 Key Learnings

### 1. API Parameter Structure
- **`/v1/crawl` vs v2**: The `/v1/crawl` endpoint in this version of Firecrawl uses a **flattened** request body. Parameters like `limit`, `maxDepth`, and `includePaths` should be at the top level of the JSON request, not nested under a `crawlerOptions` key.
- **`scrapeOptions`**: To ensure high-quality content for later processing (like RAG or AI analysis), always include `scrapeOptions` with `formats = ["markdown"]`. Markdown is significantly more token-efficient than HTML.

### 2. Identifying Target Websites
- **The Search-Then-Scrape Pattern**: Instead of jumping straight to a company URL, use `/v1/search` to find official websites. Directory sites (Clutch, Yelp, LoopNet) are great for identifying names, but `/v1/search` with a query like `"[Company Name] Tulsa official website"` is the most reliable way to get the root domain for a crawl.
- **Metadata for the Win**: Even if AI extraction (`extract` format) doesn't return the structured data you expect in the `data.extract` field, check the `data.metadata` field. Firecrawl automatically extracts titles and names into `metadata.name`, which can be used to programmatically identify companies.

### 3. Crawl Management
- **`includePaths` for Large Domains**: When crawling national or global companies (like Cushman & Wakefield), use the `includePaths` parameter to restrict the crawler to a specific sub-directory (e.g., `/en/united-states/offices/tulsa`). This prevents the crawler from wandering into thousands of irrelevant pages.
- **Polling vs Webhooks**: For local development, a simple PowerShell polling script (checking `/v1/crawl/{id}` every 10 seconds) works perfectly for managing multiple jobs.

## 🛠️ Efficient PowerShell Snippets

### Starting a Controlled Crawl
```powershell
$body = @{
    url = "https://example.com"
    limit = 50
    scrapeOptions = @{ formats = @("markdown") }
    includePaths = @("/specific-path")
} | ConvertTo-Json -Depth 10

$response = Invoke-RestMethod -Uri "http://localhost:3002/v1/crawl" -Method Post -Body $body -ContentType "application/json"
echo "Crawl ID: $($response.id)"
```

### Intelligent Result Saving
When saving crawl results, use the page title for the filename but sanitize it for the OS:
```powershell
$filename = ($page.metadata.title -replace '[^a-zA-Z0-9]', '-') + ".md"
$page.markdown | Out-File -FilePath "$subfolder/$filename"
```

## 📉 What Doesn't Work (and how to fix it)
- **Problem**: `extract` format returns empty data but uses credits.
  - **Reason**: Complex pages can cause LLM timeouts or parsing errors.
  - **Fix**: Use `markdown` format first, then pass the markdown to your own LLM call if the built-in extraction fails. This gives you more control over the prompt and context window.
- **Problem**: Shell tool timeouts.
  - **Fix**: Increase the timeout for the `Shell` tool (e.g., `timeout: 120000`) for AI-heavy operations or large crawls.

## 🌟 Optimization Checklist
- [ ] Use `markdown` for all text-based content needs.
- [ ] Set a reasonable `limit` (e.g., 50-100) for initial test runs.
- [ ] Use `includePaths` to stay on-target.
- [ ] Poll status and handle results asynchronously to save time.
