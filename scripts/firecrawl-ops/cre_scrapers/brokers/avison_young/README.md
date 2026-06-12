# Avison Young Scraper Notes

Production bulk collection has limited Avison Young support in `cre_collector/collect.ts`.

## Site Structure

- Public pages are driven by a Liferay-style app and hash-state navigation.
- Rendered sidebar/list results can be parsed, but full robust pagination has not been proven.
- Treat this Python scraper as a source lab for finding a public GET endpoint or repeatable browser action path.

## Current Limitation

Do not claim full coverage until pagination is verified against source totals and detail links across multiple pages.
