# SVN Scraper Notes

Production bulk collection uses SVN's Buildout plugin inventory feed in `cre_collector/collect.ts`.

## Buildout Feed

- Endpoint pattern: `https://buildout.com/plugins/<plugin-key>/inventory.json?page=<n>`
- SVN plugin key: `b933480474026c41d248b77156c84aef37dcac68`
- The feed returns `meta.total`, `meta.limit`, and `inventory`.
- Rows are partitioned client-side: `sale=true` for sale, otherwise lease availability.

## Rate Limits

Buildout feeds can serve HTML interstitials under sustained paging. The production collector retries individual pages and aborts the source if too many pages fail.
