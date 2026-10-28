Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# NAI native page capture

Only `CRE_NAI_NATIVE_CAPTURE_DIR` explicitly enables capture. The caller must
supply a private owner-controlled attempt directory. Each actual publicPosts
request publishes one owner-only atomic JSON page named by request offset and
SHA-256 of the complete UTF-8 JSON entry including its final newline. It carries the exact request JSON string and the
Fetch-delivered response body as base64, each with a byte count and digest,
plus request/final URL, status and observation clocks. These bytes are after
HTTP transport decoding and are not wire bytes; identity, publication authority,
visibility and completeness remain unverified. All returned data, including
GraphQL errors, stays in the body. The current query cannot capture fields not
requested by GraphQL.

The 16 MiB response ceiling and 64 KiB request ceiling are fail-closed. An
existing page is accepted only when its complete bytes exactly match; different
responses or observation clocks produce distinct entries. Failure produces a redacted
error and leaves previous pages intact. This module does not write a database,
start a worker or authorize retention. The opted-in checkpoint coordinator binds
and validates these entries, including request scope, after worker settlement.
It uses the Python acquisition converter and existing foreground evidence lane;
see [`source_evidence/README.md`](../../source_evidence/README.md) for policy,
recovery and database boundaries.

The coordinator must assert `CRE_NAI_NATIVE_CAPTURE_SINGLE_WRITER=1` only when
it owns one worker for the attempt directory. Cross-process shared publication
is unsupported and is not protected by an OS lock. Within that worker all quota
checks and publication are synchronous. `CRE_NAI_NATIVE_CAPTURE_MAX_PAGES` and
`CRE_NAI_NATIVE_CAPTURE_MAX_BYTES` may lower the defaults of 128 pages and
128 MiB of exact serialized entry bytes. Unknown, symlinked, multiply linked,
or temporary entries refuse publication rather than being ignored. Quota
refusal leaves previously published pages intact; exact replay adds no bytes.
