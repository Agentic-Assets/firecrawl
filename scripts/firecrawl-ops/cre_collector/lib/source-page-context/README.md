Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Shared source page context

`captureSourcePageContext` retains bounded native scripts, descriptions,
headings, link and image attributes, metadata and table cells from supplied HTML.
The caller supplies the source URL and optional observation clock; no fetch or
new clock is created. The caller may specify a description selector. Limits and
omission counters identify truncated item lists and text, and the existing table
helper provides its own bounds. Raw links remain source assertions; identity and
visibility are unverified. This context is not a listing qualification or an
admission decision.

Anchor context also retains supplied `title`, `aria-label`, `download`, and
`type` attributes under their original HTML names, including empty strings.
`documentLinks` retains supplied `type` and `title`. Absent optional attributes
stay absent. These values share the existing text budget and omission counters;
they are source hints for offline document discovery, not verified MIME types,
download results, original file names or authority to fetch a link. No URL is
resolved or fetched by this helper.

`isSourceChallengeShell` applies only the existing Foundry title, first-heading
and first 3,000 body-text character challenge/error signals. A false result does
not prove accessibility or absence of other challenges. Foundry's wrapper keeps
its legacy schema and its parser retains its original gates.

This shared extraction and the Lyon/Matthews integrations are drafts under the
operator's test and service hold. They are unverified and not deployed. The
250,000-character text budget, 64,000-character per-value limit and 1,000-item
per-selector cap can omit useful content; counters expose those omissions. Table
capture separately refuses HTML above 2,000,000 bytes or without a valid supplied
clock. This does not preserve original HTML bytes or prove complete extraction.
