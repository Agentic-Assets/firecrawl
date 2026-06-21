#!/usr/bin/env python3
"""
discover_buildout.py — find CRE firms on Buildout and emit onboard-ready snippets.

Buildout powers thousands of regional brokerage sites. Every Buildout site embeds a
40-hex plugin token whose public inventory API
(buildout.com/plugins/{token}/inventory.json) is exactly what collect.ts's
srcBuildout consumes. This tool turns a list of candidate CRE-firm domains into the
subset that are on Buildout, with the code to add them.

For each domain it:
  1. fetches homepage + /properties/ + /listings/ (+ /properties/for-{sale,lease}/),
  2. extracts Buildout plugin token(s) (`plugins/{40-hex}` or bare 40-hex near
     "buildout"), preferring distinct sale/lease tokens,
  3. validates each token against the public inventory API,
  4. dedups against tokens/slugs ALREADY wired in collect.ts,
  5. prints ready-to-paste BUILDOUT_FIRMS / SOURCE_KEYS / cre_ingest / SQL snippets.

The hard part is sourcing domains — there is no freely-queryable public Buildout
registry. Feed it ANY CRE-firm domain list: a broker-network export, a CoStar/CPE
list, a PublicWWW/BuiltWith `buildout.com/api.js` export, etc.

Usage:
  python3 discover_buildout.py --domains firms.txt          # one domain per line
  python3 discover_buildout.py naicapital.com foo-realty.com
  python3 discover_buildout.py --domains firms.txt --json out.json
"""
import sys, re, json, ssl, argparse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
HERE = Path(__file__).resolve().parent
COLLECT_TS = HERE / "collect.ts"


def get(url, timeout=12):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=CTX)
        return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def already_onboarded():
    """Tokens + slugs already wired in collect.ts, so we don't re-suggest them."""
    src = COLLECT_TS.read_text() if COLLECT_TS.exists() else ""
    tokens = set(re.findall(r'token:\s*"([a-f0-9]{40})"', src))
    tokens |= set(re.findall(r'buildout\.com/plugins/([a-f0-9]{40})', src))
    slugs = set(re.findall(r'^\s*"([a-z0-9-]+)"', src, re.M))
    return tokens, slugs


def test_token(t):
    b = get(f"https://buildout.com/plugins/{t}/inventory.json?page=0", timeout=20)
    try:
        d = json.loads(b)
        meta, inv = d.get("meta") or {}, d.get("inventory") or []
        if isinstance(meta.get("total"), int) and meta["total"] > 0:
            sale = sum(1 for x in inv if x.get("sale") is True)
            return {"token": t, "total": meta["total"], "sale0": sale, "lease0": len(inv) - sale}
    except Exception:
        pass
    return None


def slugify(domain):
    base = re.sub(r"^www\.", "", domain).split(".")[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def fingerprint(domain):
    domain = re.sub(r"^https?://", "", domain).strip().strip("/").lower()
    if not domain:
        return None
    blob = ""
    for p in ["", "/properties/", "/listings/", "/properties/for-sale/", "/properties/for-lease/"]:
        b = get(f"https://www.{domain}{p}")
        if not b and p == "":
            b = get(f"https://{domain}")
        blob += "\n" + b
    if not blob.strip():
        return None
    toks = set(re.findall(r"plugins/([a-f0-9]{40})", blob))
    if "buildout" in blob.lower():
        toks |= set(re.findall(r"\b([a-f0-9]{40})\b", blob))
    hits = []
    for t in list(toks)[:10]:
        r = test_token(t)
        if r:
            hits.append(r)
    if not hits:
        return None
    # de-dupe by token, keep the largest feeds
    seen, uniq = set(), []
    for h in sorted(hits, key=lambda x: -x["total"]):
        if h["token"] in seen:
            continue
        seen.add(h["token"]); uniq.append(h)
    return {"domain": domain, "slug": slugify(domain), "feeds": uniq}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domains", nargs="*")
    ap.add_argument("--domains", dest="file", help="file with one domain per line")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", help="write full results JSON here")
    args = ap.parse_args()

    doms = list(args.domains)
    if args.file:
        doms += [l.strip() for l in Path(args.file).read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    doms = list(dict.fromkeys(doms))
    if not doms:
        sys.exit("no domains given. --domains file.txt or pass domains as args.")

    known_tokens, known_slugs = already_onboarded()
    print(f"probing {len(doms)} domains ({len(known_tokens)} tokens already onboarded)...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = [r for r in ex.map(fingerprint, doms) if r]

    new = []
    for r in results:
        feeds = [f for f in r["feeds"] if f["token"] not in known_tokens]
        if not feeds or r["slug"] in known_slugs:
            continue
        r["feeds"] = feeds
        new.append(r)

    print(f"\n{len(new)} NEW Buildout firms found ({len(results)} on Buildout total):\n", file=sys.stderr)
    for r in sorted(new, key=lambda x: -max(f["total"] for f in x["feeds"])):
        feeds = " | ".join(f"{f['total']} (sale0={f['sale0']})  {f['token']}" for f in r["feeds"])
        dual = " [DUAL sale/lease tokens — wire per-tenure like franklin-street]" if len(r["feeds"]) > 1 else ""
        print(f"  {r['slug']:24} {r['domain']:28} {feeds}{dual}")
        # single-token firms: ready-to-paste BUILDOUT_FIRMS line
        if len(r["feeds"]) == 1:
            print(f'      BUILDOUT_FIRMS: "{r["slug"]}": {{ company: "{r["slug"].replace("-"," ").title()}", '
                  f'token: "{r["feeds"][0]["token"]}", page: "https://www.{r["domain"]}/" }},')

    if args.json:
        Path(args.json).write_text(json.dumps(new, indent=2))
        print(f"\nwrote {args.json}", file=sys.stderr)


if __name__ == "__main__":
    main()
