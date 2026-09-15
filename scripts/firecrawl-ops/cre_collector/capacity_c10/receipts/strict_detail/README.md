# C10 strict-detail receipt producers, batch A

These modules are deliberately source-local receipt adapters. They are not a
registry, CLI, collector extension, or live executor. A later governed
coordinator must bind a private receipt root, an immutable plan/cohort/policy,
and an injected direct provider transport before any request can be made.

| Source | Native proof path | Wave 4 state |
| --- | --- | --- |
| `jll` | GraphQL enumeration, public `__NEXT_DATA__` detail | sealed one-shot producer; not fully verified |
| `jll-investor` | United States Next search, build-bound structured detail | sealed one-shot producer; not fully verified |
| `colliers` | RCM ordered map/list pair, SLP detail | sealed one-shot producer; not fully verified |
| `marcus-millichap` | Content-search POST, activity-bound map detail POST | sealed one-shot producer; not fully verified |
| `avison-young` | Browser-equivalent detail evidence | explicit blocker; no lower-fidelity feed receipt |
| `colliers-main` | Browser/stealth challenge-cleared detail evidence | explicit blocker; no lower-fidelity sitemap receipt |

For every implemented producer, source-native enumeration first proves the
selected provider identities and canonical routes. It then appends every member
request from a sealed parent projection and freezes the full member graph before
any member request may execute. Each source card has one direct attempt,
`no-store`, no redirects, and no fallback. The public receipts carry hashes and
accounting only; raw responses, URLs, POST bodies, and parsed evidence stay in
the private 0700 receipt store.

The blockers are intentional. They must not be registered as
`fully_verified`, and they may only be replaced by a source-specific,
offline-proven ephemeral one-attempt renderer with equivalent identity and asset
evidence. No module here imports collection orchestration, normal scrape
helpers, cache helpers, database/status/scheduler paths, or model/OCR controls.
