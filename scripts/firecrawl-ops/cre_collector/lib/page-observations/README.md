Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Source page observations

`--observe-input` accepts a private JSON file with `items`, an array of 1–100
objects with `sourceKey`, an absolute clean HTTPS `url`, optional `sourceId`, and
optional JSON `context`. All items must share one known source. `--out` is
required; collection, enrichment and concurrency flags are incompatible. Pages
are scraped serially with one client dispatch per attempted input; this does
not count Firecrawl's internal fetches. `--out` names a new private directory;
each ordered `part-NNNN.json` is a standard mode-0600 collector artifact and
`run-summary.json` contains only run counts and part names.
It is diagnostic, not an authenticated receipt. Parts are published after
collection; a crash before publication can leave an empty directory. An absent
summary does not establish a completed run or remote settlement.
Input identity and context are unverified; missing IDs use the URL as a witness.
Context over 64 KiB is explicitly omitted. Existing evidence limits apply to
individual fields, while observations are split across parts instead of dropped.
Part counts cover that part; summary counts cover the run. Captured clocks are collector clocks, not
proof of publisher freshness. Metadata-only results and failures remain evidence.
An absent or null source ID remains distinct in the input evidence. Requested,
attempted, returned, failed and unattempted counts describe the batch; returned
and failed can overlap if evidence capture fails after a response. These counts
do not establish source coverage.
The URL check rejects obvious local hostnames and literal IP addresses; it does
not establish that DNS resolves publicly. The existing Firecrawl request guard
still applies. An access denial or an error with uncertain remote settlement
stops later items, which are retained as unattempted observations.
An exact common challenge phrase in the HTML title or first heading also stops
the batch as a possible challenge, without asserting access rights. Incidental
contact-form CAPTCHA widgets are not a challenge signal.

From the collector directory, for a private input prepared by the caller:

```sh
npx tsx collect.ts --observe-input /private/path/input.json --out /private/path/new-output-directory
```

The artifact contains no listings or brokers, has `mode: observe` and
`evidenceOnly: true`, and does not complete queue claims or qualify content as a
canonical property. Local private capture is not database admission: run the
existing source-evidence scanner and admission policy before persistence.
