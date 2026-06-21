export const meta = {
  name: 'claude-md-maintenance',
  description: 'Hierarchy-aware audit, dedup, and refinement of every CLAUDE.md guidance file. Self-discovers inputs, then audits + arbitrates + (apply mode) edits files in small per-subtree GROUPS (a handful of files per agent), not one agent per file.',
  phases: [
    { title: 'Discover', detail: 'find every CLAUDE.md, line counts, always-on set, coverage gaps' },
    { title: 'Analyze', detail: 'per-subtree group audit: accuracy/staleness/duplication/altitude' },
    { title: 'Coverage', detail: 'one agent judges every candidate dir for a new CLAUDE.md' },
    { title: 'Arbitrate', detail: 'global dedup ledger + altitude moves', model: 'opus' },
    { title: 'Remediate', detail: 'apply edits per group, hubs first then modules' },
    { title: 'Report', detail: 'synthesize findings + write report', model: 'opus' },
  ],
}

// ---------------------------------------------------------------------------
// Inputs. Heavy data is SELF-DISCOVERED (Phase 0) so the workflow does not
// depend on large args being delivered. args only carries optional overrides:
//   mode       : 'apply' (audit+arbitrate+edit) | 'review' (no edits)
//   reportPath : where the Report agent writes the markdown summary
//   groupSize  : max files per group agent (default 8)
// args may arrive as an object or a JSON string; handle both.
// ---------------------------------------------------------------------------
let opts = args
if (typeof opts === 'string') {
  try {
    opts = JSON.parse(opts)
  } catch {
    opts = {}
  }
}
opts = opts || {}
const mode = opts.mode === 'review' ? 'review' : 'apply'
const reportPath = opts.reportPath || 'docs/reports/claude-md-maintenance-report.md'
const MAX_GROUP = Math.max(3, Math.min(12, opts.groupSize || 8))
const ALWAYS_ON_BATCH = 4
const PROTECTED = new Set([
  'docs/goals-northstar-strategies/CLAUDE.md',
  'src/components/chat/CLAUDE.md',
])
const SCOPE_GUARD =
  'SCOPE GUARD: read freely, but EDIT/WRITE ONLY the specific CLAUDE.md file(s) named as targets in this task (and, for coverage, the one new CLAUDE.md you choose to create). Never modify any other file, never touch tasks/**, never run git commands.'

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------
const FILE_ANALYSIS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    lineCount: { type: 'integer' },
    healthScore: { type: 'integer', description: '0-100 overall freshness/quality/concision' },
    summary: { type: 'string', description: '1-2 sentence verdict' },
    staleOrInaccurate: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          quote: { type: 'string' },
          problem: { type: 'string' },
          fix: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
        required: ['quote', 'problem', 'fix', 'severity'],
      },
    },
    brokenRefs: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ref: { type: 'string' },
          kind: { type: 'string', enum: ['path', 'symbol', 'command', 'env', 'route', 'other'] },
          suggestion: { type: 'string' },
        },
        required: ['ref', 'kind', 'suggestion'],
      },
    },
    duplications: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          quote: { type: 'string' },
          duplicatesFile: { type: 'string', description: 'ancestor/always-on/sibling file that already states this' },
          recommendation: {
            type: 'string',
            enum: ['remove_here_keep_above', 'condense_to_pointer', 'move_up', 'keep_both_justified'],
          },
          note: { type: 'string' },
        },
        required: ['quote', 'duplicatesFile', 'recommendation', 'note'],
      },
    },
    altitudeIssues: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          quote: { type: 'string' },
          issue: {
            type: 'string',
            enum: ['too_global_for_leaf', 'too_specific_for_hub', 'belongs_in_child', 'belongs_in_parent'],
          },
          suggestion: { type: 'string' },
        },
        required: ['quote', 'issue', 'suggestion'],
      },
    },
    concision: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          quote: { type: 'string' },
          action: { type: 'string', enum: ['cut', 'tighten', 'merge_bullets'] },
          note: { type: 'string' },
        },
        required: ['quote', 'action', 'note'],
      },
    },
    coverageNote: { type: 'string', description: 'genuinely-missing guidance that SHOULD be here (or empty)' },
    verdict: {
      type: 'string',
      enum: ['keep_as_is', 'minor_edits', 'significant_edits', 'merge_up_candidate', 'split_candidate'],
    },
  },
  required: ['path', 'healthScore', 'summary', 'verdict'],
}

const GROUP_ANALYSIS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    files: { type: 'array', items: FILE_ANALYSIS_SCHEMA, description: 'one analysis per target file in the group' },
  },
  required: ['files'],
}

const DISCOVERY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    files: { type: 'array', items: { type: 'string' }, description: 'every CLAUDE.md, repo-relative, no leading ./' },
    lineCounts: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: { path: { type: 'string' }, lines: { type: 'integer' } },
        required: ['path', 'lines'],
      },
    },
    alwaysOn: {
      type: 'array',
      items: { type: 'string' },
      description: 'root CLAUDE.md plus every CLAUDE.md it @-imports (loaded into every session)',
    },
    coverageCandidates: {
      type: 'array',
      items: { type: 'string' },
      description: 'dirs with substantial non-test code and NO CLAUDE.md that may warrant one',
    },
  },
  required: ['files', 'lineCounts', 'alwaysOn', 'coverageCandidates'],
}

const COVERAGE_DECISION_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    dir: { type: 'string' },
    recommend: { type: 'string', enum: ['add', 'skip'] },
    coveredByAncestorAlready: { type: 'boolean' },
    rationale: { type: 'string' },
    parentTableNeedsRow: { type: 'string', description: 'parent CLAUDE.md whose folder-ownership table should gain a row, or empty' },
    wrote: { type: 'boolean', description: 'true if you actually wrote the new file (apply mode)' },
    estLines: { type: 'integer' },
  },
  required: ['dir', 'recommend', 'rationale'],
}

const COVERAGE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    decisions: { type: 'array', items: COVERAGE_DECISION_SCHEMA },
  },
  required: ['decisions'],
}

const ARBITRATION_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    ledger: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          topic: { type: 'string' },
          canonicalHome: { type: 'string', description: 'the single file that should own this content' },
          removeFrom: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: { file: { type: 'string' }, quote: { type: 'string' } },
              required: ['file', 'quote'],
            },
          },
          action: { type: 'string', enum: ['remove_dupes', 'condense_to_pointer', 'move_up', 'leave_as_is'] },
          rationale: { type: 'string' },
        },
        required: ['topic', 'canonicalHome', 'removeFrom', 'action', 'rationale'],
      },
    },
    altitudeMoves: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          content: { type: 'string' },
          fromFile: { type: 'string' },
          toFile: { type: 'string' },
          rationale: { type: 'string' },
        },
        required: ['content', 'fromFile', 'toFile', 'rationale'],
      },
    },
    priorityFixes: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          file: { type: 'string' },
          what: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
        },
        required: ['file', 'what', 'severity'],
      },
    },
    crossCuttingObservations: { type: 'array', items: { type: 'string' } },
  },
  required: ['ledger'],
}

const FILE_REMEDIATE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    file: { type: 'string' },
    edited: { type: 'boolean' },
    linesBefore: { type: 'integer' },
    linesAfter: { type: 'integer' },
    changesApplied: { type: 'array', items: { type: 'string' } },
    removedDuplications: { type: 'array', items: { type: 'string' } },
    fixedStale: { type: 'array', items: { type: 'string' } },
    preservedDespiteFlag: { type: 'array', items: { type: 'string' }, description: 'flags deliberately NOT acted on, with why' },
    note: { type: 'string' },
  },
  required: ['file', 'edited'],
}

const GROUP_REMEDIATE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    files: { type: 'array', items: FILE_REMEDIATE_SCHEMA, description: 'one remediation per target file in the group' },
  },
  required: ['files'],
}

// ---------------------------------------------------------------------------
// Shared rubric
// ---------------------------------------------------------------------------
const RUBRIC = `EQUIRE uses layered CLAUDE.md guidance files under strict context precedence: when an agent works inside a directory, the harness loads root CLAUDE.md, then each ancestor directory's CLAUDE.md, then the local one. Root ALSO @-imports a fixed "always-on" set into EVERY session. So whatever sits in an ancestor or always-on file is ALREADY in context for a deeper file; repeating it lower down is wasted context budget, paid on every turn.

The altitude principle:
- Higher-level files describe how subsystems CONNECT, cross-cutting rules, and pointers to the module docs that hold detail.
- Deeper files hold module-specific contracts, invariants, gotchas, and canonical helper names NOT already stated above.
- Good files are concise, current, and load-bearing. No filler, no restating global rules, no stale references.`

// ---------------------------------------------------------------------------
// Phase 0: Discover inputs (one Bash-capable agent).
// ---------------------------------------------------------------------------
log(`mode=${mode} · groupSize<=${MAX_GROUP} · discovering inputs...`)
phase('Discover')
const discovery =
  (await agent(
    `Inventory EQUIRE's CLAUDE.md guidance files. Use Bash (find, wc, rg, grep) only; read-only, no edits.

1. files: every CLAUDE.md tracked in the repo, excluding node_modules and .git. Return repo-relative paths with NO leading "./".
2. lineCounts: for each file, its line count (wc -l).
3. alwaysOn: root "CLAUDE.md" PLUS every CLAUDE.md it @-imports. Find them by reading the root CLAUDE.md "Key Module Context (auto-loaded)" section and collecting the lines beginning with "@" (strip the leading "@"). Include "CLAUDE.md" itself in this list.
4. coverageCandidates: directories that have substantial NON-test code but NO CLAUDE.md and may warrant one. Compute: for each dir under src/, count direct .ts/.tsx files; keep dirs with >= 8 direct code files, no CLAUDE.md, and whose path does NOT contain "__tests__". Exclude pure test/fixture/mock dirs. Return the top ~15 by file count, repo-relative, no leading "./".

Return the structured inventory exactly.`,
    { label: 'discover:inventory', phase: 'Discover', agentType: 'general-purpose', model: 'sonnet', schema: DISCOVERY_SCHEMA }
  )) || { files: [], lineCounts: [], alwaysOn: [], coverageCandidates: [] }

const files = discovery.files || []
const lineMap = new Map((discovery.lineCounts || []).map((x) => [x.path, x.lines]))
const ALWAYS_ON = new Set(discovery.alwaysOn || [])
const coverageCandidates = (discovery.coverageCandidates || []).slice(0, 15)
const fileSet = new Set(files)

if (!files.length) {
  log('discovery returned no files; aborting')
  return { mode, error: 'discovery_failed', filesAnalyzed: 0, edited: 0 }
}

// ---------------------------------------------------------------------------
// Hierarchy helpers (pure, from the discovered file set).
// ---------------------------------------------------------------------------
const short = (p) => p.replace(/\/CLAUDE\.md$/, '').replace(/^src\//, '') || 'root'
const dirOf = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')

function treeAncestors(file) {
  const out = []
  let d = dirOf(file)
  while (true) {
    const cand = d ? d + '/CLAUDE.md' : 'CLAUDE.md'
    if (cand !== file && fileSet.has(cand)) out.push(cand)
    if (!d) break
    d = dirOf(d)
  }
  return out
}
function contextAbove(file) {
  const s = new Set([...ALWAYS_ON, ...treeAncestors(file)])
  s.delete(file)
  return [...s]
}
function tierOf(file) {
  if (file === 'CLAUDE.md' || ALWAYS_ON.has(file)) return 'A-alwaysOn'
  if (
    file.startsWith('docs/research/') ||
    file.startsWith('docs/soc2-compliance/') ||
    file.startsWith('docs/realcomm-notes/') ||
    file.startsWith('docs/goals-northstar-strategies/') ||
    file.startsWith('src/app/(mockups)/')
  )
    return 'H-historical'
  return 'C-module'
}

// ---------------------------------------------------------------------------
// Grouping: a handful of files per agent, kept within one subtree region so
// each agent sees siblings (and often their shared parent) together. The
// always-on hubs are isolated into their own Opus groups; everything else is
// region-chunked Sonnet. depth-min lets us order remediation hubs-first.
// ---------------------------------------------------------------------------
function regionOf(file) {
  if (file === 'CLAUDE.md') return '(root)'
  const parts = file.split('/')
  if (parts[0] === 'src' && parts.length >= 3) return parts[0] + '/' + parts[1]
  return parts[0]
}
function chunk(arr, size) {
  const out = []
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size))
  return out
}
function minDepth(fileList) {
  return Math.min(...fileList.map((f) => f.split('/').length))
}

const sorted = [...files].sort()
const alwaysOnFiles = sorted.filter((f) => ALWAYS_ON.has(f))
const moduleFiles = sorted.filter((f) => !ALWAYS_ON.has(f))

const labelCount = new Map()
function nextLabel(base) {
  const n = (labelCount.get(base) || 0) + 1
  labelCount.set(base, n)
  return `${base}#${n}`
}

const groups = []
for (const batch of chunk(alwaysOnFiles, ALWAYS_ON_BATCH)) {
  groups.push({ tier: 'alwaysOn', model: 'opus', files: batch, region: 'always-on', label: nextLabel('always-on'), depth: minDepth(batch) })
}
const byRegion = new Map()
for (const f of moduleFiles) {
  const r = regionOf(f)
  if (!byRegion.has(r)) byRegion.set(r, [])
  byRegion.get(r).push(f)
}
for (const [region, rf] of byRegion) {
  for (const batch of chunk(rf, MAX_GROUP)) {
    groups.push({ tier: 'module', model: 'sonnet', files: batch, region, label: nextLabel(region), depth: minDepth(batch) })
  }
}

log(
  `discovered ${files.length} files · ${ALWAYS_ON.size} always-on · ${groups.length} groups ` +
    `(${groups.filter((g) => g.tier === 'alwaysOn').length} opus / ${groups.filter((g) => g.tier === 'module').length} sonnet) · ` +
    `${coverageCandidates.length} coverage candidates`
)

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------
function fileBlock(file) {
  const above = contextAbove(file)
  const hist = tierOf(file) === 'H-historical' ? ' [HISTORICAL: be conservative; only flag genuinely wrong/stale facts; do not push dedup/altitude]' : ''
  return `- ${file}  (tier: ${tierOf(file)})${hist}
    already-in-context above this file (do NOT repeat their content; flag duplications of these): ${above.length ? above.join(', ') : '(none)'}`
}

function GROUP_ANALYZE_PROMPT(group) {
  return `You are auditing a GROUP of ${group.files.length} CLAUDE.md guidance files from the same area of the EQUIRE repo. Return findings ONLY (no edits this round). ${SCOPE_GUARD}

${RUBRIC}

These files are relatives in one subtree, so ALSO check intra-group duplication: a deeper file in this group repeating content already stated by a shallower file in this SAME group (not just by the always-on/ancestor list).

TARGET FILES (audit EACH; return exactly one entry per file in "files", with path set to the file):
${group.files.map(fileBlock).join('\n')}

For EACH target file:
1. Read it in full.
2. Detect duplication vs its above-context files AND vs shallower files in this group. grep/skim ancestors for overlapping phrases/rules rather than reading them end to end; quote the exact overlap and name the file that already states it.
3. Verify the 6-12 most load-bearing concrete references (file paths, exported symbol/function names, commands, env vars, route paths) with rg/glob/read. Report any that no longer resolve, with a corrected suggestion. Prefer rg over guessing.
4. Flag stale/outdated claims (wrong versions, removed features, superseded patterns, renamed symbols, dated statements no longer true).
5. Flag altitude problems (content too global for a leaf and duplicated above; or hub content drifted into one child's micro-detail) and within-file bloat that can be tightened without losing meaning.
6. Note any genuinely-missing guidance an agent in that directory would need but the file omits.

Quote exact offending text. Do NOT flag a critical safety rule, a canonical helper/contract name, a required-lockstep checklist, or a protected-spec note as "duplication" merely because it also appears above; load-bearing local emphasis can be legitimate. When unsure, lean keep. Accuracy and safety beat brevity.

Return one analysis object per target file.`
}

function COVERAGE_PROMPT() {
  const writeClause =
    mode === 'apply'
      ? `For each dir you recommend "add" AND are confident about, WRITE the new <dir>/CLAUDE.md now (Write tool, that one file only) with a concise body, set wrote=true. For "skip", write nothing and set wrote=false.`
      : `Do NOT write any file (review mode). For "add", describe the intended file in rationale and set wrote=false.`
  return `Decide, for EACH candidate directory below, whether it warrants its OWN concise CLAUDE.md. ${SCOPE_GUARD}

${RUBRIC}

CANDIDATE DIRECTORIES:
${coverageCandidates.map((d) => '- ' + d).join('\n')}

For each dir: inspect what's in it and whether its nearest ANCESTOR CLAUDE.md (walk up to find it) ALREADY covers it adequately.
Recommend "add" ONLY when ALL hold: substantial non-test, non-trivial code with conventions an agent would otherwise miss; those conventions are NOT already covered by an ancestor; and a short file (aim 15-45 lines) would measurably help. Recommend "skip" otherwise (thin/obvious dirs, __tests__ without a non-obvious harness convention, or anything an ancestor already explains). Most dirs should be skip or already-covered.
If adding under a parent that keeps a folder-ownership table (e.g. src/components/CLAUDE.md), set parentTableNeedsRow to that parent path.
A good new file: tight headers, module-specific contracts/gotchas/canonical helpers, pointers up for cross-cutting rules, ZERO restatement of ancestor content, sibling CLAUDE.md voice and format, no em dashes. ${writeClause}

Return one decision per candidate dir in "decisions".`
}

function ARBITRATE_PROMPT(compact) {
  return `You are the lead context-architect arbitrating cross-file duplication and altitude across ALL of EQUIRE's CLAUDE.md files. The per-group auditors flagged overlaps within their own subtrees; you decide, globally and consistently, the SINGLE canonical home for each piece of guidance and who must drop or condense it. This is analysis only; do not edit files.

${RUBRIC}

Principles:
- Canonical home = the HIGHEST file where the rule is broadly true. Cross-cutting/global rules belong in root or the relevant always-on hub; module-specific rules belong in that module's file.
- A rule duplicated across siblings usually belongs in their shared parent; children keep at most a one-line pointer if navigation needs it.
- A terse reminder pointing to a canonical helper in a SIBLING tree (not an ancestor, so not auto-loaded) is acceptable; prefer condense_to_pointer over deletion there.
- Never strip a genuine safety/compliance rule from a file where an agent must see it without it being guaranteed above. When in doubt, condense_to_pointer, not remove.
- Focus on dedup that spans DIFFERENT groups/subtrees; intra-subtree dedup is handled by the group editors themselves.
- Be decisive but conservative: only ledger entries you are confident about. Quote the exact text to remove from each file verbatim.

Per-file flags (JSON):
${compact}

Produce: a dedup ledger (topic, canonicalHome, removeFrom[file+quote], action, rationale), altitude moves (content, fromFile, toFile), priority fixes (high-severity stale/broken refs worth calling out), and a few cross-cutting observations.`
}

function GROUP_REMEDIATE_PROMPT(group, items) {
  return `Refine a GROUP of ${items.length} CLAUDE.md file(s) by APPLYING the agreed edits. Edit each file in place (Edit/Write). Make each accurate, current, correctly-altituded, and concise WITHOUT losing any genuine guidance. ${SCOPE_GUARD}

${RUBRIC}

Process the files PARENT-FIRST (shallower path before deeper) so any content moved up lands before a child drops it. Handle exactly these files and nothing else:

${items
  .map(
    (it) => `=== ${it.file} ===
PER-FILE AUDIT FINDINGS (apply the valid ones):
${JSON.stringify(it.analysis)}
GLOBAL DEDUP LEDGER - REMOVE/CONDENSE from THIS file (owned by their canonical home elsewhere; replace with a one-line pointer only if navigation needs it):
${it.removals.length ? JSON.stringify(it.removals) : '(none)'}
GLOBAL LEDGER - THIS file is the canonical home for these (ensure present and clear here):
${it.additions.length ? JSON.stringify(it.additions) : '(none)'}`
  )
  .join('\n\n')}

RULES:
- Read each file first. Apply stale/broken-ref fixes ONLY after verifying the replacement actually exists (rg/glob). If you cannot verify a fix, leave the original and record it in preservedDespiteFlag.
- Remove true cross-level duplication; keep load-bearing local emphasis. When unsure whether something is dup vs needed, KEEP it.
- NEVER delete a safety/compliance rule, a canonical helper/contract name, a protected-spec note, or a required-lockstep checklist. Do not change meaning, only tighten and de-duplicate.
- Do NOT delete a file or leave it empty. If a file was marked merge_up/split, just tighten it and note the recommendation; a human handles structural merges/deletes.
- Preserve heading structure and each file's voice; match sibling CLAUDE.md formatting. Never add em dashes; keep existing house style.
- Reduce lines only where it improves clarity; do not pad.

Return one remediation object per file in "files".`
}

// ---------------------------------------------------------------------------
// Phase 1+2: Analyze groups (barrier for arbitration) and Coverage, concurrently.
// ---------------------------------------------------------------------------
phase('Analyze')
const [groupAnalysesRaw, coverageResult] = await Promise.all([
  parallel(
    groups.map((g) => () =>
      agent(GROUP_ANALYZE_PROMPT(g), {
        label: `analyze:${g.label}`,
        phase: 'Analyze',
        agentType: 'general-purpose',
        model: g.model,
        schema: GROUP_ANALYSIS_SCHEMA,
      })
    )
  ),
  coverageCandidates.length
    ? agent(COVERAGE_PROMPT(), {
        label: 'coverage:gap-scan',
        phase: 'Coverage',
        agentType: 'general-purpose',
        model: 'sonnet',
        schema: COVERAGE_SCHEMA,
      })
    : Promise.resolve({ decisions: [] }),
])

const analyses = groupAnalysesRaw.filter(Boolean).flatMap((g) => g.files || [])
const coverage = (coverageResult && coverageResult.decisions) || []
const analysisByPath = new Map(analyses.map((a) => [a.path, a]))
log(`analyzed ${analyses.length}/${files.length} files across ${groups.length} groups · coverage decisions ${coverage.length}`)

// ---------------------------------------------------------------------------
// Phase 3: Arbitrate (Opus). Feed only cross-file-relevant flags, compacted.
// ---------------------------------------------------------------------------
phase('Arbitrate')
const compact = JSON.stringify(
  analyses
    .map((a) => ({
      file: a.path,
      tier: tierOf(a.path),
      contextAbove: contextAbove(a.path),
      duplications: a.duplications || [],
      altitudeIssues: a.altitudeIssues || [],
      topStale: (a.staleOrInaccurate || []).filter((s) => s.severity === 'high').slice(0, 3),
    }))
    .filter((x) => x.duplications.length || x.altitudeIssues.length || x.topStale.length)
)
const arbitration =
  (await agent(ARBITRATE_PROMPT(compact), {
    label: 'arbitrate:dedup-ledger',
    phase: 'Arbitrate',
    agentType: 'general-purpose',
    model: 'opus',
    schema: ARBITRATION_SCHEMA,
  })) || { ledger: [], altitudeMoves: [], priorityFixes: [], crossCuttingObservations: [] }

const removalsByFile = new Map()
const additionsByFile = new Map()
const pushTo = (map, key, val) => {
  if (!map.has(key)) map.set(key, [])
  map.get(key).push(val)
}
for (const entry of arbitration.ledger || []) {
  if (entry.action === 'leave_as_is') continue
  for (const r of entry.removeFrom || []) {
    if (r.file === entry.canonicalHome) continue
    pushTo(removalsByFile, r.file, { topic: entry.topic, quote: r.quote, action: entry.action, canonicalHome: entry.canonicalHome, rationale: entry.rationale })
  }
  pushTo(additionsByFile, entry.canonicalHome, { topic: entry.topic, rationale: entry.rationale })
}
for (const m of arbitration.altitudeMoves || []) {
  pushTo(removalsByFile, m.fromFile, { topic: 'altitude-move', quote: m.content, action: 'move_up', canonicalHome: m.toFile, rationale: m.rationale })
  pushTo(additionsByFile, m.toFile, { topic: 'altitude-move', rationale: m.rationale + ' (moved from ' + m.fromFile + ')' })
}
log(`ledger: ${(arbitration.ledger || []).length} entries · altitude moves: ${(arbitration.altitudeMoves || []).length} · priority fixes: ${(arbitration.priorityFixes || []).length}`)

// ---------------------------------------------------------------------------
// Phase 4: Remediate (apply mode only). Reuse the SAME groups. Drop protected
// and no-work files; run always-on hub groups first, then module groups.
// ---------------------------------------------------------------------------
let remediations = []
if (mode === 'apply') {
  phase('Remediate')
  const buildItems = (g) =>
    g.files
      .filter((f) => !PROTECTED.has(f))
      .map((f) => {
        const a = analysisByPath.get(f) || { path: f, summary: '(no analysis returned)', verdict: 'minor_edits' }
        const removals = removalsByFile.get(f) || []
        const additions = additionsByFile.get(f) || []
        const noWork =
          a.verdict === 'keep_as_is' &&
          !removals.length &&
          !additions.length &&
          !(a.staleOrInaccurate || []).length &&
          !(a.brokenRefs || []).length &&
          !(a.concision || []).length &&
          !(a.altitudeIssues || []).length
        return { file: f, analysis: a, removals, additions, noWork }
      })
      .filter((it) => !it.noWork)

  const remediableGroups = groups
    .map((g) => ({ ...g, items: buildItems(g) }))
    .filter((g) => g.items.length)

  for (const tier of ['alwaysOn', 'module']) {
    const wave = remediableGroups.filter((g) => g.tier === tier)
    if (!wave.length) continue
    log(`remediate ${tier}: ${wave.length} groups · ${wave.reduce((n, g) => n + g.items.length, 0)} files`)
    const res = await parallel(
      wave.map((g) => () =>
        agent(GROUP_REMEDIATE_PROMPT(g, g.items), {
          label: `remediate:${g.label}`,
          phase: 'Remediate',
          agentType: 'general-purpose',
          model: g.model,
          schema: GROUP_REMEDIATE_SCHEMA,
        })
      )
    )
    remediations.push(...res.filter(Boolean).flatMap((r) => r.files || []))
  }
  log(`remediated: ${remediations.filter((r) => r.edited).length} edited / ${remediations.length} processed`)
}

// ---------------------------------------------------------------------------
// Phase 5: Report (Opus) - write the markdown summary and return it.
// ---------------------------------------------------------------------------
phase('Report')
const stats = {
  totalFiles: files.length,
  analyzed: analyses.length,
  groups: groups.length,
  verdicts: analyses.reduce((m, a) => ((m[a.verdict] = (m[a.verdict] || 0) + 1), m), {}),
  highStale: analyses.flatMap((a) => (a.staleOrInaccurate || []).filter((s) => s.severity === 'high').map((s) => ({ file: a.path, ...s }))),
  brokenRefs: analyses.flatMap((a) => (a.brokenRefs || []).map((b) => ({ file: a.path, ...b }))),
  mergeUp: analyses.filter((a) => a.verdict === 'merge_up_candidate').map((a) => a.path),
  splitCandidates: analyses.filter((a) => a.verdict === 'split_candidate').map((a) => a.path),
  lowHealth: analyses.filter((a) => typeof a.healthScore === 'number' && a.healthScore < 70).map((a) => ({ file: a.path, score: a.healthScore, summary: a.summary })),
  coverageAdds: coverage.filter((c) => c.recommend === 'add').map((c) => ({ dir: c.dir, wrote: !!c.wrote, estLines: c.estLines, parentTableNeedsRow: c.parentTableNeedsRow })),
  edited: remediations.filter((r) => r.edited).length,
  remediations: remediations.map((r) => ({ file: r.file, edited: r.edited, linesBefore: r.linesBefore, linesAfter: r.linesAfter, changes: (r.changesApplied || []).length })),
}
const reportInput = JSON.stringify({ mode, stats, ledger: arbitration.ledger, altitudeMoves: arbitration.altitudeMoves, crossCutting: arbitration.crossCuttingObservations, priorityFixes: arbitration.priorityFixes })
const report = await agent(
  `Write a tight, skimmable markdown report for a maintenance run over EQUIRE's ${files.length} CLAUDE.md files (audited in ${groups.length} per-subtree groups), then WRITE it to "${reportPath}" (create the directory with Bash mkdir -p if needed). Write ONLY that one report file; do not edit anything else. Return the same markdown as your output.

Sections: (1) one-paragraph executive summary; (2) what changed this run (files edited, dedupes applied, coverage files added) ${mode === 'apply' ? '' : '(review mode: nothing was edited)'}; (3) top stale/broken references fixed or flagged, as a table grouped by area; (4) the dedup/altitude ledger as a table (topic, canonical home, removed-from, action); (5) coverage: new CLAUDE.md added or recommended, and any dirs deliberately skipped; (6) files flagged for HUMAN follow-up (merge_up / split candidates / protected files / unverifiable fixes); (7) 3-6 cross-cutting observations about the guidance system's health. Be concrete, cite file paths, no fluff, no em dashes.

DATA (JSON):
${reportInput}`,
  { label: 'report:synthesis', phase: 'Report', agentType: 'general-purpose', model: 'opus' }
)

return {
  mode,
  reportPath,
  filesAnalyzed: analyses.length,
  groups: groups.length,
  edited: stats.edited,
  coverageFilesAdded: stats.coverageAdds.filter((c) => c.wrote).length,
  coverageRecommended: stats.coverageAdds.length,
  ledgerEntries: (arbitration.ledger || []).length,
  altitudeMoves: (arbitration.altitudeMoves || []).length,
  mergeUpCandidates: stats.mergeUp,
  splitCandidates: stats.splitCandidates,
  highStaleCount: stats.highStale.length,
  brokenRefCount: stats.brokenRefs.length,
  report,
}
