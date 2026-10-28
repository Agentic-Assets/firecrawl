Part of [AGENTIC-2895](https://linear.app/agenticassets/issue/AGENTIC-2895).

# Offline Buildout observation projection

`buildout_observation_projection.py` is an isolated, offline preparation tool.
It reads a previously built private observation artifact, its original input,
and a matching manifest. It produces a new private file; it does not contact
providers, access a database, update listings, schedule work, or authorize
production ingestion.

The caller must supply **all** of the following:

- `--input`: the exact private artifact bytes produced by
  `buildout_observation_artifact.py`.
- `--original`: the exact saved input bytes used to build that artifact.
- `--manifest`: the corresponding manifest from
  `buildout_observation_manifest.py seal`.
- `--source-key`, `--generation-id`, `--capture-id`, `--page`, and
  `--transaction`: the expected scope, exactly matching the manifest.
- `--out`: a new private output path. Existing files and final symlinks are
  refused; the new file is created with mode `0600`.

The projector verifies the original, artifact, manifest, builder fingerprint,
and asserted scope before writing any output. This is **byte-consistency
verification**, not a signature or proof of publisher origin, capture time,
listing identity, or permission to collect or write. A different input and its
newly created unsigned manifest can also be internally consistent. Maintain
the original evidence under the applicable private retention rules.

The manifest transaction is an operator assertion. It does not prove the
tenure of any row. The projection retains all rows without filtering by that
assertion and marks each `rowTransaction` as `unverified`; listing identity,
freshness, visibility, source availability, and source completeness also
remain unresolved or unverified. Promotion is explicitly not authorized.

The projector derives a space-count candidate only from one unambiguous
`# of Spaces` literal supported by the existing parser. Duplicate, malformed,
quarantined, or omitted evidence never yields a candidate. A `Cap Rate` value
is retained as a literal only; this tool does not parse or promote it. The
artifact, original input, manifest, and projection are private unreviewed
data, not safe-to-share or secret-free output. Do not commit them or feed them
to the collector or ingestor. The CLI prints only aggregate counts.

Run the synthetic contracts from a separate `git clone --no-hardlinks`:

```sh
python3 -m pytest -q tests/test_buildout_observation_projection.py
```
