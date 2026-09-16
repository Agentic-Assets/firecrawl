# Candidate inventory verifiers

These eight modules are sealed-evidence validators for the source-native
inventory parsers already in `sources/`.  They make no request, cache, status,
database, scheduler, model, or OCR mutation.  Their `fully_verified` flags are
deliberately `False`. More importantly, the canonical repository authority
approves no cohort or adapter, so neither importing nor mutating a candidate
object can admit a source.

The matching TypeScript package now has candidate receipt producers for seven
sources. CBRE Deal Flow is an explicit blocked descriptor until its
provider-derived engine-key and form-urlencoded POST/HTML protocol can be
represented faithfully. The executable candidates can construct source-owned, sealed request cards and
projections only when a future coordinator provides a concrete direct-provider
transport. A separate reviewer may add an adapter to a future registry only
after verifying its complete population path, asset/field fidelity, a genuine
source-native attrition signal, and the real no-write transport. The existing
generic HTTP path, canonical cache, and write-capable collector paths are not
an integration target. No inventory source has been admitted or run.

| Source | Native population surface | Candidate blocker |
| --- | --- | --- |
| cbre | two-pass public listings API | receipt producer and independent review |
| cbre-dealflow | ListingEngine form POST plus HTML cards | blocked: engine-key/form POST/HTML protocol is not yet sealed |
| cushman-wakefield | public API offsets | receipt producer and independent review |
| newmark | NIM ascending search feed | receipt producer and independent review |
| srs | Cloud Run property search | receipt producer and independent review |
| svn | Buildout `inventory.json` | receipt producer and independent review |
| lee-associates | Buildout `inventory.json` | receipt producer and independent review |
| bull-realty | Buildout `inventory.json` | receipt producer and independent review |

None has a reviewed not-found classifier in this wave.  All return `False` for
every purported tombstone input, including an HTTP 404, so lifecycle action
remains outside this package.
