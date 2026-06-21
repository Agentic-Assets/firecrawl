#!/usr/bin/env python3
"""REST-based ingestor for the CRE collector — lands staged listings into the
`credeals` schema via the Supabase REST API + SUPABASE_SERVICE_ROLE_KEY, WITHOUT a
postgres password.

Why this exists: the EQUIRE app authenticates to Supabase over REST with the
service-role key, not a direct postgres connection, and the stored postgres
passwords are stale everywhere (local + Vercel prod). So `cre_ingest.py` (psql +
COPY) can't connect. This tool reuses `cre_ingest.py --dry-run` to produce the
exact normalized staging, then replays it over REST.

It is faithful to cre_ingest's mapping (same _stage columns, same child handling),
with ONE difference forced by PostgREST: the unique index on cre_listings is
PARTIAL (`(brokerage_id, external_id) WHERE external_id IS NOT NULL`), which
PostgREST can't target for upsert. So per in-scope brokerage we clean-slate DELETE
then plain INSERT. That equals a full reconcile for those firms — SAFE for new or
full-inventory runs only. Refuses to wipe a brokerage that already has listings
unless --replace is given.

Usage:
  python3 cre_ingest_rest.py --in out/run.json                 # dry summary
  python3 cre_ingest_rest.py --in out/run.json --go            # write
  python3 cre_ingest_rest.py --ingest-sql /tmp/x/ingest.sql --go
  python3 cre_ingest_rest.py --in out/run.json --go --only-slug=hanley   # validate one firm
"""
import re, json, sys, os, urllib.request, urllib.error, time, argparse, subprocess, tempfile

HERE=os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROJECT="https://fhqycqubkkrdgzswccwd.supabase.co"
ENV_CANDIDATES=[
    os.path.expanduser("~/Dropbox/CRE_EQUIRE/.env.local"),
    os.path.expanduser("~/Dropbox/dynamically-display-cre-listing-data/.env.local"),
    os.path.expanduser("~/Dropbox/firecrawl_aa/.env.local"),
    "/tmp/equire_live.env",
]

ap=argparse.ArgumentParser()
g=ap.add_mutually_exclusive_group(required=True)
g.add_argument("--in", dest="infile", help="collector JSON; staging is produced via cre_ingest.py --dry-run")
g.add_argument("--ingest-sql", help="pre-generated cre_ingest --keep-artifacts ingest.sql")
ap.add_argument("--env-file", help="explicit env file for SUPABASE_SERVICE_ROLE_KEY/SUPABASE_URL")
ap.add_argument("--go", action="store_true", help="actually write (default: dry summary)")
ap.add_argument("--replace", action="store_true", help="allow clean-slate replace of brokerages that already have listings")
ap.add_argument("--only-slug"); ap.add_argument("--limit", type=int, default=0)
args=ap.parse_args()

def load_env():
    files=[args.env_file] if args.env_file else ENV_CANDIDATES
    sk=url=None
    for f in files:
        if not f or not os.path.exists(f): continue
        for line in open(f):
            m=re.match(r'\s*([A-Z0-9_]+)\s*=\s*["\']?([^"\'\s]+)', line)
            if not m: continue
            k,v=m.group(1),m.group(2)
            if k in ("SUPABASE_SERVICE_ROLE_KEY","SERVICE_ROLE_KEY","SUPABASE_SERVICE_KEY") and not sk: sk=v
            if k in ("SUPABASE_URL","NEXT_PUBLIC_SUPABASE_URL") and not url: url=v
        if sk: break
    return sk, (url or DEFAULT_PROJECT)

SK, SUPA = load_env()
if not SK:
    sys.exit("ERROR: no SUPABASE_SERVICE_ROLE_KEY found in: "+", ".join(c for c in (ENV_CANDIDATES if not args.env_file else [args.env_file])))
BASE=SUPA.rstrip("/")+"/rest/v1"
H={"apikey":SK,"Authorization":f"Bearer {SK}","Content-Type":"application/json"}

def req(method, path, body=None, prefer=None, params=""):
    h=dict(H); h["Accept-Profile"]="credeals"; h["Content-Profile"]="credeals"
    if prefer: h["Prefer"]=prefer
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(f"{BASE}/{path}{params}", data=data, headers=h, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=90) as resp:
                raw=resp.read().decode(); return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            msg=e.read().decode()[:200]
            if e.code in (502,503,504) and attempt<3: time.sleep(2*(attempt+1)); continue
            return e.code, msg
        except Exception as e:
            if attempt<3: time.sleep(2*(attempt+1)); continue
            return "ERR", str(e)[:140]

# --- obtain ingest.sql staging ---
ingest_sql=args.ingest_sql
tmpdir=None
if args.infile:
    tmpdir=tempfile.mkdtemp(prefix="cre_rest_")
    print(f"running cre_ingest.py --dry-run to stage {args.infile} ...")
    p=subprocess.run([sys.executable, os.path.join(HERE,"cre_ingest.py"), "--in", args.infile,
                      "--dry-run", "--keep-artifacts", tmpdir], capture_output=True, text=True)
    if p.returncode!=0: sys.exit("cre_ingest dry-run failed:\n"+p.stdout[-800:]+p.stderr[-800:])
    ingest_sql=os.path.join(tmpdir,"ingest.sql")
    print(p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "")
sql=open(ingest_sql).read()

# --- parse _stage ---
def _cols(name):
    m=re.search(r'COPY '+name+r'\s*\(([^)]*)\)\s*FROM stdin;', sql); return [c.strip() for c in m.group(1).split(',')]
def _rows(name):
    m=re.search(r'COPY '+name+r'[^\n]*FROM stdin;\n(.*?)\n\\\.', sql, re.S); return [r for r in m.group(1).split('\n') if r!='']
def unesc(f):
    if f==r'\N': return None
    o=[];i=0
    while i<len(f):
        c=f[i]
        if c=='\\' and i+1<len(f): o.append({'t':'\t','n':'\n','r':'\r','\\':'\\'}.get(f[i+1],f[i+1])); i+=2
        else: o.append(c); i+=1
    return ''.join(o)
SC=_cols('_stage'); SR=_rows('_stage')
NUMF={'lat','lng','size_sf','lot_size_sf','sale_price_usd','sale_price_per_sf','cap_rate','lease_rate_min','lease_rate_max'}
INTF={'year_built'}; JSONF={'raw_data','contacts','documents','images'}
def conv(col,val):
    if val is None: return None
    if col in NUMF:
        try: return float(val)
        except: return None
    if col in INTF:
        try: return int(float(val))
        except: return None
    if col in JSONF:
        try: return json.loads(val)
        except: return None
    return val
staged=[{c:conv(c,unesc(v)) for c,v in zip(SC, line.split('\t'))} for line in SR]
if args.only_slug: staged=[d for d in staged if d['slug']==args.only_slug]
if args.limit: staged=staged[:args.limit]
slugs=sorted(set(d['slug'] for d in staged))
print(f"staged: {len(staged)} listings across {len(slugs)} firms -> {SUPA}")

# --- brokerage seed ---
brk={}
for d in staged:
    s=d['slug']
    if s in brk: continue
    name=(d.get('raw_data') or {}).get('sourceCompany') or s
    o=re.match(r'(https?://[^/]+)', d.get('source_url') or '')
    brk[s]={"slug":s,"name":name,"base_url":o.group(1) if o else None,"search_url":o.group(1) if o else None,"active":True}

if not args.go:
    print(f"DRY: would upsert {len(brk)} brokerages + {len(staged)} listings (+children). Use --go.")
    sys.exit(0)

st,_=req("POST","cre_brokerages", list(brk.values()), prefer="resolution=merge-duplicates,return=minimal", params="?on_conflict=slug")
print("brokerage upsert ->", st)
st,bdata=req("GET","cre_brokerages", params="?select=id,slug&slug=in.("+",".join(slugs)+")")
idmap={b['slug']:b['id'] for b in bdata}
missing=[s for s in slugs if s not in idmap]
if missing: sys.exit("ERROR: brokerages missing after upsert: "+", ".join(missing))

# --- safety: refuse to wipe firms that already have listings unless --replace ---
scope_ids=sorted(set(idmap[s] for s in slugs))
st,existing=req("GET","cre_listings", params="?select=brokerage_id&deleted_at=is.null&brokerage_id=in.("+",".join(scope_ids)+")&limit=100000")
have={}
for r in (existing or []): have[r['brokerage_id']]=have.get(r['brokerage_id'],0)+1
nonempty={k:v for k,v in have.items() if v}
if nonempty and not args.replace:
    rev={v:k for k,v in idmap.items()}
    sys.exit("ABORT: these in-scope brokerages already have listings (use --replace for a full reconcile):\n  "
             +", ".join(f"{rev.get(bid,bid)}={n}" for bid,n in nonempty.items()))

dst,_=req("DELETE","cre_listings", params="?brokerage_id=in.("+",".join(scope_ids)+")", prefer="return=minimal")
print(f"clean-slate delete for {len(scope_ids)} brokerages -> {dst}")

# --- listings ---
LC=['external_id','source_url','transaction_type','property_type','title','address','city','state','zip','lat','lng','size_sf','lot_size_sf','year_built','sale_price_usd','sale_price_per_sf','cap_rate','lease_rate_min','lease_rate_max','description','updated_date','scraped_at','raw_data']
key2id={}; ok=0; fail=0; B=500
for i in range(0,len(staged),B):
    recs=[]
    for d in staged[i:i+B]:
        r={c:d.get(c) for c in LC}; r['brokerage_id']=idmap[d['slug']]; r['status']='active'; r['deleted_at']=None
        recs.append(r)
    st,resp=req("POST","cre_listings", recs, prefer="return=representation", params="?select=id,brokerage_id,external_id")
    if st in (200,201) and isinstance(resp,list):
        ok+=len(resp)
        for r in resp: key2id[(r['brokerage_id'], r['external_id'])]=r['id']
    else:
        fail+=len(recs); print(f"  listings batch {i//B} -> {st} {str(resp)[:160]}")
    print(f"  listings {min(i+B,len(staged))}/{len(staged)} (ok={ok})")
print(f"listings: {ok} inserted, {fail} failed, {len(key2id)} ids captured")

# --- children (new/replaced listings => clean) ---
def collect(kind):
    out=[]
    for d in staged:
        lid=key2id.get((idmap[d['slug']], d['external_id']))
        if not lid: continue
        arr=d.get(kind)
        if isinstance(arr,list):
            for x in arr:
                if isinstance(x,dict): out.append((lid,x))
    return out
cont=[{"listing_id":l,"name":x.get('name'),"title":x.get('title'),"email":x.get('email'),"phone":x.get('phone'),"brokerage_name":x.get('company'),"is_primary":bool(x.get('isPrimary',False))} for l,x in collect('contacts')]
docs=[{"listing_id":l,"doc_type":(x.get('docType') if x.get('docType') in ('brochure','om','flyer','floor_plan') else 'other'),"title":x.get('title'),"url":x.get('url')} for l,x in collect('documents') if x.get('url')]
imgs=[{"listing_id":l,"url":x.get('url'),"is_primary":bool(x.get('isPrimary',False)),"display_order":int(x.get('order') or 0)} for l,x in collect('images') if x.get('url')]
def post_children(table, recs):
    n=0
    for i in range(0,len(recs),1000):
        st,resp=req("POST",table,recs[i:i+1000],prefer="return=minimal")
        if st in (200,201): n+=len(recs[i:i+1000])
        else: print(f"  {table} batch {i//1000} -> {st} {str(resp)[:120]}")
    return n
print(f"children: contacts={post_children('cre_listing_contacts',cont)} documents={post_children('cre_listing_documents',docs)} images={post_children('cre_listing_images',imgs)}")
print("DONE")
