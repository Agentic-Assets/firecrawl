#!/usr/bin/env python3
"""Mechanical, verbatim splitter for cre_collector/collect.ts.

Reads the verified pre-refactor backup, partitions the file body into
per-declaration line spans (leading comments attached to the following
declaration), assigns each declaration to a target module, and emits modules
with auto-computed imports. The ONLY injected lines are import/export and a
one-line header comment. Run with --write to emit files; default is dry-run.
"""
import os
import re
import sys

ROOT = "/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/scripts/firecrawl-ops/cre_collector"
BACKUP = "/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/tasks/tmp/cre-track1-baseline-2026-06-13/collect.ts.preRefactor"
DECLS = "/Users/caymanseagraves/Documents/GitHub/agentic-assets/firecrawl/tasks/tmp/cre-track1-baseline-2026-06-13/decls.preRefactor.txt"
BODY_START = 27  # first body declaration (API_URL); lines 1-26 are header+imports (dropped/replaced)

WRITE = "--write" in sys.argv

with open(BACKUP) as f:
    raw = f.read()
lines = raw.split("\n")  # lines[i] is 1-indexed via lines_1[i]; build 1-indexed list
# 1-indexed access: L[1].. ; L[0] unused
L = [None] + lines  # L[1..len(lines)]
N = len(lines)
# If file ends with newline, lines[-1]=='' ; last real code line is N-1.
LAST = N
while LAST > 0 and L[LAST] == "":
    LAST -= 1  # last non-empty line index (5759 == '});')

DECL_RE = re.compile(r"^(?:async\s+)?(?:function|const|let|type|interface)\b")
NAME_RE = re.compile(r"^(?:async\s+)?(?:function|const|let|type|interface)\s+([A-Za-z_$][\w$]*)")
DESTRUCT_RE = re.compile(r"^const\s*\{")

def is_comment_or_blank(s):
    t = s.strip()
    return t == "" or t.startswith("//")

# --- detect declaration starts in body ---
decls = []  # list of (lineno, name)
for i in range(BODY_START, LAST + 1):
    s = L[i]
    if DECL_RE.match(s):
        m = NAME_RE.match(s)
        if m:
            name = m.group(1)
        elif DESTRUCT_RE.match(s):
            name = "flags"
        else:
            raise SystemExit(f"decl-start with no name at line {i}: {s!r}")
        decls.append((i, name))

names = [n for (_, n) in decls]
# --- validate against baseline ---
baseline = set()
with open(DECLS) as f:
    for ln in f:
        ln = ln.rstrip("\n")
        if not ln.strip():
            continue
        m = NAME_RE.match(ln)
        if not m:
            raise SystemExit(f"baseline line unparsed: {ln!r}")
        baseline.add(m.group(1))

LETS = {"newmarkCreds", "avisonYoungCache", "colliersMainSitemapCache", "colliersMainEnrichedMemo"}
expected = baseline | LETS | {"flags"}
got = set(names)
if got != expected:
    print("NAME MISMATCH")
    print("  missing (in baseline, not detected):", sorted(baseline - got))
    print("  extra   (detected, not expected)  :", sorted(got - expected))
    raise SystemExit(1)
if len(names) != len(set(names)):
    raise SystemExit("duplicate decl names detected")
print(f"decls detected: {len(decls)} (baseline {len(baseline)} + lets {len(LETS)} + flags)")

# --- compute spans (leading comments attach to following decl) ---
decl_lines = [d[0] for d in decls]
block_start = {}
for idx, (dl, nm) in enumerate(decls):
    j = dl - 1
    while j >= BODY_START and is_comment_or_blank(L[j]):
        j -= 1
    bs = j + 1
    if idx == 0:
        bs = BODY_START
    block_start[dl] = bs
# block ends
spans = {}  # decl_line -> (start, end, name)
for idx, (dl, nm) in enumerate(decls):
    start = block_start[dl]
    end = (block_start[decls[idx + 1][0]] - 1) if idx + 1 < len(decls) else LAST
    spans[dl] = (start, end, nm)

# --- verbatim partition assertion: concat all spans in order == body lines ---
recon = []
for dl, nm in decls:
    s, e, _ = spans[dl]
    recon.extend(L[s:e + 1])
orig_body = L[BODY_START:LAST + 1]
if recon != orig_body:
    # find first diff
    for k in range(min(len(recon), len(orig_body))):
        if recon[k] != orig_body[k]:
            print("FIRST DIFF at recon idx", k, repr(recon[k]), "vs", repr(orig_body[k]))
            break
    raise SystemExit(f"PARTITION MISMATCH len recon={len(recon)} body={len(orig_body)}")
print(f"verbatim partition OK: {len(recon)} body lines covered ({BODY_START}..{LAST})")

# --- module assignment ---
TYPES = {"Tx", "ScrapeOpts", "ScrapedDoc", "SourceResult"}
UTIL = {"clean", "num", "boundedInt", "moneyToNumber", "isPerSfPriceText", "prune", "pmap"}
SCRAPE = {"firecrawl", "scrapeRaw", "scrapeDoc", "parseJsonBody", "repairUnescapedJsonStringQuotes", "scrapeJson"}
BROKER = {"brokerIndex", "brokers", "brokerRef"}
HTML = {"decodeHtmlEntities", "titleFromFilename", "jsonLdObjects", "firstJsonLd", "stripHtmlText", "dedupeStrings", "extractSitemapUrlEntries"}
CONFIG = {"API_URL", "flags", "PAGE_CAP", "CONCURRENCY", "OUT_PATH"}
COLLECT = {"SOURCE_KEYS", "SourceKey", "sourceArg", "requestedSources", "txArg", "TRANSACTIONS", "rawMax", "MAX_ITEMS", "MONITOR", "UNSUPPORTED", "runSource", "main"}

# section ranges by decl-start line (used when name not in an explicit override set)
SECTIONS = [
    (357, 478, "cbre"),
    (479, 893, "buildout"),
    (894, 1118, "newmark"),
    (1119, 1582, "jll"),
    (1583, 1932, "jllInvestor"),
    (1933, 2343, "cushman"),
    (2344, 2739, "marcus"),
    (2740, 3213, "avison"),
    (3214, 3581, "savills"),
    (3582, 3839, "nai"),
    (3840, 4304, "cbreDealflow"),
    (4305, 4709, "colliers"),
    (4710, 5249, "colliersMain"),
    (5250, 5581, "transwestern"),
]

def assign(dl, nm):
    if nm in TYPES: return "types"
    if nm in UTIL: return "util"
    if nm in SCRAPE: return "scrape"
    if nm in BROKER: return "broker"
    if nm in HTML: return "html"
    if nm in CONFIG: return "config"
    if nm in COLLECT: return "collect"
    for lo, hi, mod in SECTIONS:
        if lo <= dl <= hi:
            return mod
    raise SystemExit(f"unassigned decl {nm} at line {dl}")

# module metadata: key -> (subdir, filename)
MODMETA = {
    "types": ("", "types.ts"),
    "config": ("lib", "config.ts"),
    "scrape": ("lib", "scrape.ts"),
    "util": ("lib", "util.ts"),
    "broker": ("lib", "broker.ts"),
    "html": ("lib", "html.ts"),
    "collect": ("", "collect.ts"),
    "cbre": ("sources", "cbre.ts"),
    "buildout": ("sources", "buildout.ts"),
    "newmark": ("sources", "newmark.ts"),
    "jll": ("sources", "jll.ts"),
    "jllInvestor": ("sources", "jll-investor.ts"),
    "cushman": ("sources", "cushman-wakefield.ts"),
    "marcus": ("sources", "marcus-millichap.ts"),
    "avison": ("sources", "avison-young.ts"),
    "savills": ("sources", "savills.ts"),
    "nai": ("sources", "nai-global.ts"),
    "cbreDealflow": ("sources", "cbre-dealflow.ts"),
    "colliers": ("sources", "colliers.ts"),
    "colliersMain": ("sources", "colliers-main.ts"),
    "transwestern": ("sources", "transwestern.ts"),
}

name_module = {}
mod_decls = {k: [] for k in MODMETA}
for dl, nm in decls:
    mod = assign(dl, nm)
    name_module[nm] = mod
    mod_decls[mod].append((dl, nm))

# report assignment
print("\n--- module decl counts ---")
for k in MODMETA:
    print(f"  {k:14s} {len(mod_decls[k]):3d}  -> {MODMETA[k][0]+'/' if MODMETA[k][0] else ''}{MODMETA[k][1]}")

def relimport(importer_mod, target_mod):
    isub, _ = MODMETA[importer_mod]
    tsub, tfile = MODMETA[target_mod]
    tbase = tfile[:-3]  # strip .ts
    if isub == tsub:
        return f"./{tbase}.js"
    if isub == "" and tsub == "lib":
        return f"./lib/{tbase}.js"
    if isub == "" and tsub == "sources":
        return f"./sources/{tbase}.js"
    if isub in ("lib", "sources") and tsub == "":
        return f"../{tbase}.js"
    if isub == "sources" and tsub == "lib":
        return f"../lib/{tbase}.js"
    if isub == "lib" and tsub == "sources":
        return f"../sources/{tbase}.js"
    raise SystemExit(f"relimport unhandled {importer_mod}->{target_mod}")

FS_FNS = ["appendFileSync", "existsSync", "mkdirSync", "readFileSync", "renameSync", "writeFileSync"]

def build_module(mod):
    sub, fname = MODMETA[mod]
    own = set(n for _, n in mod_decls[mod])
    decls_sorted = sorted(mod_decls[mod], key=lambda x: x[0])
    body_lines = []
    for dl, nm in decls_sorted:
        s, e, _ = spans[dl]
        for i in range(s, e + 1):
            if i == dl and mod != "collect":
                body_lines.append("export " + L[i])
            else:
                body_lines.append(L[i])
    _raw = []
    for dl, _ in decls_sorted:
        s2, e2, _ = spans[dl]
        _raw.extend(L[s2:e2 + 1])
    raw_body_text = "\n".join(_raw)
    # for reference detection only: strip // line comments (guarding :// in URLs)
    det_lines = [re.sub(r"(?<![:/])//.*$", "", ln) for ln in _raw]
    body_text = "\n".join(det_lines)

    # cross-module imports (never import from the entry module `collect`)
    by_mod = {}
    for other_name, other_mod in name_module.items():
        if other_mod == mod or other_mod == "collect" or other_name in own:
            continue
        if re.search(r"\b" + re.escape(other_name) + r"\b", body_text):
            by_mod.setdefault(other_mod, set()).add(other_name)

    import_lines = []
    # third-party
    if mod == "scrape":
        import_lines.append('import Firecrawl from "@mendable/firecrawl-js";')
    if re.search(r"\bcheerio\b", body_text):
        import_lines.append('import * as cheerio from "cheerio";')
    if mod == "config":
        import_lines.append('import { parseArgs } from "node:util";')
    # node builtins
    fs_used = [fn for fn in FS_FNS if re.search(r"\b" + fn + r"\b", body_text)]
    if fs_used:
        import_lines.append(f'import {{ {", ".join(fs_used)} }} from "node:fs";')
    if re.search(r"\bdirname\b", body_text):
        import_lines.append('import { dirname } from "node:path";')
    if re.search(r"\bcreateHash\b", body_text):
        import_lines.append('import { createHash } from "node:crypto";')
    # local cross-module
    src_to_src = []
    for tmod in sorted(by_mod):
        syms = ", ".join(sorted(by_mod[tmod]))
        path = relimport(mod, tmod)
        import_lines.append(f"import {{ {syms} }} from \"{path}\";")
        if sub == "sources" and MODMETA[tmod][0] == "sources":
            src_to_src.append((tmod, sorted(by_mod[tmod])))

    header = f"// {sub + '/' if sub else ''}{fname} - extracted verbatim from collect.ts (see tasks/tmp backup)"
    out = header + "\n"
    if import_lines:
        out += "\n".join(import_lines) + "\n"
    out += "\n" + "\n".join(body_lines) + "\n"
    return out, src_to_src, sorted(by_mod.keys())

# build all, detect source->source
print("\n--- imports per module ---")
problems = []
files = {}
for mod in MODMETA:
    text, s2s, deps = build_module(mod)
    files[mod] = text
    print(f"  {mod:14s} deps: {deps}")
    if s2s:
        problems.append((mod, s2s))

if problems:
    print("\n!!! SOURCE->SOURCE IMPORTS DETECTED:")
    for mod, s2s in problems:
        print("   ", mod, "->", s2s)
    raise SystemExit("source-to-source coupling; relocate shared helper to lib")
print("\nNo source-to-source imports. OK.")

if WRITE:
    os.makedirs(os.path.join(ROOT, "lib"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "sources"), exist_ok=True)
    for mod in MODMETA:
        sub, fname = MODMETA[mod]
        path = os.path.join(ROOT, sub, fname) if sub else os.path.join(ROOT, fname)
        with open(path, "w") as f:
            f.write(files[mod])
        print("wrote", path)
else:
    print("\nDRY RUN (pass --write to emit files)")
