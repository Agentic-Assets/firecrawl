import Firecrawl from "@mendable/firecrawl-js";

const apiKey = process.env.FIRECRAWL_API_KEY;
if (!apiKey) {
  console.error("FIRECRAWL_API_KEY is not set");
  process.exit(1);
}
const firecrawl = new Firecrawl({ apiKey });

const SITE = "us-comm";
const PAGE_SIZE = 200;
const CONCURRENCY = 5;
const BASE = "https://www.cbre.com";
const ASSET_BASE = `${BASE}/resources/fileassets/`;
const DETAIL_BASE = `${BASE}/properties/properties-for-lease/commercial-space/details/`;
const SOURCE_URL =
  "https://www.cbre.com/properties/properties-for-lease/commercial-space?aspects=isSale";

function apiUrl(page: number): string {
  return (
    `${BASE}/listings-api/propertylistings/query` +
    `?site=${SITE}&Common.Aspects=isSale&PageSize=${PAGE_SIZE}&Page=${page}`
  );
}

async function fetchApiPage(page: number): Promise<any> {
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const doc = await firecrawl.scrape(apiUrl(page), {
        formats: ["rawHtml"],
        integration: "prometheus",
      });
      const body = (doc as any).rawHtml ?? "";
      let parsed: any;
      try {
        parsed = JSON.parse(body);
      } catch {
        // the JSON body may come wrapped in an HTML shell — cut to the outermost braces
        const start = body.indexOf("{");
        const end = body.lastIndexOf("}");
        if (start === -1 || end === -1) {
          throw new Error("response from CBRE listings API contained no JSON object");
        }
        parsed = JSON.parse(body.slice(start, end + 1));
      }
      if (typeof parsed.DocumentCount !== "number" || !Array.isArray(parsed.Documents)) {
        throw new Error("CBRE listings API response is missing DocumentCount/Documents fields");
      }
      return parsed;
    } catch (err) {
      lastErr = err;
      console.error(`page ${page} attempt ${attempt} failed: ${err}`);
      await new Promise((r) => setTimeout(r, 2500 * attempt));
    }
  }
  throw lastErr;
}

// ---------- helpers ----------

function clean(s: any): string | null {
  if (typeof s !== "string") return null;
  const t = s.trim();
  return t || null;
}

function num(v: any): number | null {
  return typeof v === "number" && isFinite(v) && v !== 0 ? v : null;
}

// localized text arrays: [{Common.CultureCode, Common.Text}]
function text(localized: any): string | null {
  if (Array.isArray(localized) && localized.length > 0) {
    const en =
      localized.find((t: any) => t["Common.CultureCode"] === "en-US") ?? localized[0];
    return clean(en?.["Common.Text"]);
  }
  return null;
}

function slugify(parts: Array<string | null>): string {
  return parts
    .filter(Boolean)
    .join(" ")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function absUrl(u: string | null): string | null {
  if (!u) return null;
  if (/^https?:\/\//i.test(u)) return u;
  if (u.startsWith("/")) return `${BASE}${u}`;
  return u;
}

// File assets live at {ASSET_BASE}{listingId}/{path}. Store the per-listing path to
// keep 5,900 listings within the delivery size limit; full URL = assetBaseUrl + id + "/" + path.
function assetPath(u: string | null, id: string): string | null {
  const full = absUrl(u);
  if (!full) return null;
  const prefix = `${ASSET_BASE}${id}/`;
  return full.startsWith(prefix) ? full.slice(prefix.length) : full;
}

function imageSetPath(p: any, id: string): string | null {
  const resources = Array.isArray(p?.["Common.ImageResources"])
    ? p["Common.ImageResources"]
    : [];
  const original =
    resources.find((r: any) => r["Common.Breakpoint"] === "original") ?? resources[0];
  if (!original) return null;
  return assetPath(
    clean(original["Common.Resource.Uri"]) ?? clean(original["Source.Uri"]),
    id
  );
}

function normalizeCharge(c: any): any {
  return {
    kind: clean(c["Common.ChargeKind"]),
    amount: num(c["Common.Amount"]),
    currency: clean(c["Common.CurrencyCode"]),
    interval: clean(c["Common.Interval"]),
    perUnit: clean(c["Common.PerUnit"]),
    onApplication: c["Common.OnApplication"] === true,
    exact: c["Common.Exact"] === true,
  };
}

// Strip uninformative values: null/undefined, empty strings/arrays/objects, false flags.
function prune(v: any): any {
  if (v === null || v === undefined || v === false || v === "") return undefined;
  if (Array.isArray(v)) {
    const arr = v.map(prune).filter((x) => x !== undefined);
    return arr.length ? arr : undefined;
  }
  if (typeof v === "object") {
    const out: Record<string, any> = {};
    for (const [k, val] of Object.entries(v)) {
      const p = prune(val);
      if (p !== undefined) out[k] = p;
    }
    return Object.keys(out).length ? out : undefined;
  }
  return v;
}

// URLs in fields not already captured by a dedicated output field.
const CAPTURED_ELSEWHERE = /Photos|PrimaryImage|FloorPlans|Avatar|Brochures|BrochureUrl|Walkthrough|Website/i;

function sweepUrls(node: any, path: string, found: Set<string>): void {
  if (typeof node === "string") {
    const t = node.trim();
    if (!/^https?:\/\//i.test(t) && !t.startsWith("/resources/") && !t.startsWith("/-/media"))
      return;
    if (CAPTURED_ELSEWHERE.test(path)) return;
    found.add(absUrl(t)!);
    return;
  }
  if (Array.isArray(node)) {
    node.forEach((v, i) => sweepUrls(v, `${path}[${i}]`, found));
    return;
  }
  if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node)) sweepUrls(v, path ? `${path}.${k}` : k, found);
  }
}

// ---------- broker dedupe table ----------

const brokerIndex = new Map<string, number>();
const brokers: any[] = [];

function brokerRef(b: {
  name: string | null;
  email: string | null;
  phone: string | null;
  office: string | null;
  avatarUrl: string | null;
}): number | null {
  if (!b.name && !b.email) return null;
  const key = (b.email ?? "") + "|" + (b.name ?? "");
  const existing = brokerIndex.get(key);
  if (existing !== undefined) {
    const rec = brokers[existing];
    // enrich an earlier record if this occurrence has more detail
    if (!rec.phone && b.phone) rec.phone = b.phone;
    if (!rec.office && b.office) rec.office = b.office;
    if (!rec.avatarUrl && b.avatarUrl) rec.avatarUrl = b.avatarUrl;
    return existing;
  }
  const idx = brokers.length;
  brokers.push({ ...b, company: "CBRE" });
  brokerIndex.set(key, idx);
  return idx;
}

// ---------- per-listing normalization ----------

function toListing(d: any): any {
  const id = d["Common.PrimaryKey"];
  const addr = d["Common.ActualAddress"] ?? {};
  const name = clean(addr["Common.Line1"]);
  const street = clean(addr["Common.Line2"]);
  const line3 = clean(addr["Common.Line3"]);
  const city = clean(addr["Common.Locallity"]);
  const state = clean(addr["Common.Region"]);
  const postalCode = clean(addr["Common.PostCode"]);
  const country = clean(addr["Common.Country"]);
  const slug = slugify([name, street, line3, city, state, postalCode]);

  const aspects: string[] = Array.isArray(d["Common.Aspects"]) ? d["Common.Aspects"] : [];
  const availability = d["Common.Availability"] ?? {};

  const highlights = (Array.isArray(d["Common.Highlights"]) ? d["Common.Highlights"] : [])
    .map((h: any) => text(h["Common.Highlight"]))
    .filter(Boolean);

  const charges: any[] = Array.isArray(d["Common.Charges"]) ? d["Common.Charges"] : [];
  const saleCharge = charges.find(
    (c) => c["Common.ChargeKind"] === "SalePrice" && num(c["Common.Amount"])
  );
  const otherCharges = charges.filter((c) => c !== saleCharge).map(normalizeCharge);

  const coord = d["Common.Coordinate"] ?? {};
  const parking = d["Common.Parking"] ?? {};

  const amenities = Object.entries(d)
    .filter(
      ([k, v]) =>
        v === true &&
        (/^Dynamic\.(Has|Is)/.test(k) ||
          ["Dynamic.Furnished", "Dynamic.NewHome", "Dynamic.DevelopmentOpportunity"].includes(k))
    )
    .map(([k]) => k.replace(/^Dynamic\./, ""));

  // merge the two contact lists (Agents + ContactGroup) into the broker table
  const byKey = new Map<string, any>();
  for (const a of Array.isArray(d["Common.Agents"]) ? d["Common.Agents"] : []) {
    const email = clean(a["Common.EmailAddress"]);
    const nm = clean(a["Common.AgentName"]);
    byKey.set((email ?? "") + "|" + (nm ?? ""), {
      name: nm,
      email,
      phone: clean(a["Common.TelephoneNumber"]),
      office: clean(a["Common.AgentOffice"]),
      avatarUrl: null,
    });
  }
  const groupContacts = d["Common.ContactGroup"]?.["Common.Contacts"];
  for (const a of Array.isArray(groupContacts) ? groupContacts : []) {
    const email = clean(a["Common.EmailAddress"]);
    const nm = clean(a["Common.AgentName"]);
    const key = (email ?? "") + "|" + (nm ?? "");
    const rec = byKey.get(key) ?? {
      name: nm,
      email,
      phone: clean(a["Common.TelephoneNumber"]),
      office: null,
      avatarUrl: null,
    };
    rec.avatarUrl = rec.avatarUrl ?? absUrl(clean(a["Common.Avatar"]));
    byKey.set(key, rec);
  }
  const brokerIds = Array.from(byKey.values())
    .map(brokerRef)
    .filter((x): x is number => x !== null);

  const brochures = (Array.isArray(d["Common.Brochures"]) ? d["Common.Brochures"] : []).map(
    (b: any) => ({
      name: clean(b["Common.BrochureName"]),
      path: assetPath(clean(b["Common.Uri"]), id),
    })
  );

  const photos = (Array.isArray(d["Common.Photos"]) ? d["Common.Photos"] : [])
    .map((p: any) => imageSetPath(p, id))
    .filter(Boolean);
  const floorPlans = (Array.isArray(d["Common.FloorPlans"]) ? d["Common.FloorPlans"] : [])
    .map((p: any) => imageSetPath(p, id))
    .filter(Boolean);

  const floorsAndUnits = (
    Array.isArray(d["Common.FloorsAndUnits"]) ? d["Common.FloorsAndUnits"] : []
  ).map((u: any) => ({
    name: text(u["Common.SubdivisionName"]),
    use: clean(u["Common.Unit.Use"]),
    status: clean(u["Common.Unit.Status"]),
    areas: (Array.isArray(u["Common.Areas"]) ? u["Common.Areas"] : []).map((a: any) => ({
      area: num(a["Common.Area"]),
      units: clean(a["Common.Units"]),
    })),
    charges: (Array.isArray(u["Common.Charges"]) ? u["Common.Charges"] : []).map(normalizeCharge),
  }));

  const demographics = (
    Array.isArray(d["Common.Demographics"]) ? d["Common.Demographics"] : []
  ).map((g: any) => ({
    category: clean(g["Common.Category"]),
    units: clean(g["Common.DistanceUnits"]),
    byRadius: (Array.isArray(g["Common.StatisticsData"]) ? g["Common.StatisticsData"] : []).map(
      (s: any) => [s["Common.Interval"], s["Common.Amount"]]
    ),
  }));

  const transportation = (
    Array.isArray(d["Common.TransportationTypes"]) ? d["Common.TransportationTypes"] : []
  ).map((t: any) => ({
    type: clean(t["Common.Type"]),
    places: (Array.isArray(t["Common.Places"]) ? t["Common.Places"] : []).map((p: any) => ({
      name: text(p["Common.Name"]),
      distance: (Array.isArray(p["Common.Distances"]) ? p["Common.Distances"] : [])
        .map((x: any) => `${x["Common.Amount"]} ${x["Common.DistanceUnits"]}`)
        .join(", "),
    })),
  }));

  // Common.Sizes flattened: sqft TotalSize always equals Dynamic.TotalArea, so only
  // the acre figures and minimum size add information.
  let totalSizeAcres: number | null = null;
  let minimumSizeAcres: number | null = null;
  let minimumSizeSqft: number | null = null;
  for (const s of Array.isArray(d["Common.Sizes"]) ? d["Common.Sizes"] : []) {
    const kind = clean(s["Common.SizeKind"]);
    for (const x of Array.isArray(s["Common.Dimensions"]) ? s["Common.Dimensions"] : []) {
      const units = clean(x["Common.DimensionsUnits"]);
      const amount = num(x["Common.Amount"]);
      if (kind === "TotalSize" && units === "acre") totalSizeAcres = amount;
      if (kind === "MinimumSize" && units === "acre") minimumSizeAcres = amount;
      if (kind === "MinimumSize" && units === "sqft") minimumSizeSqft = amount;
    }
  }

  const otherUrls = new Set<string>();
  sweepUrls(d, "", otherUrls);

  return prune({
    id,
    name,
    headline: text(d["Common.Strapline"]),
    alsoForLease: aspects.includes("isLetting"),
    assetType: clean(d["Common.UsageType"]),
    status: clean(d["Common.Status"]),
    availabilityKind:
      clean(availability["Common.AvailabilityKind"]) === "AvailableFromKnownDate"
        ? null
        : clean(availability["Common.AvailabilityKind"]),
    availabilityDate: clean(availability["Common.Date"]),
    description: text(d["Common.LongDescription"]),
    locationDescription: text(d["Common.LocationDescription"]),
    highlights,
    street,
    addressLine3: line3,
    city,
    state,
    postalCode,
    country: country === "US" ? null : country,
    latitude: typeof coord.lat === "number" ? coord.lat : null,
    longitude: typeof coord.lon === "number" ? coord.lon : null,
    yearBuilt: num(d["Common.YearBuilt"]),
    slug,
    salePriceUsd: saleCharge ? saleCharge["Common.Amount"] : null,
    salePriceCurrency:
      saleCharge && clean(saleCharge["Common.CurrencyCode"]) !== "USD"
        ? clean(saleCharge["Common.CurrencyCode"])
        : null,
    salePriceApproximate: saleCharge ? saleCharge["Common.Exact"] !== true : false,
    priceOnApplication: !saleCharge,
    otherCharges,
    leaseRateType: clean(d["Common.LeaseRateType"]),
    leaseTypes: Array.isArray(d["Common.LeaseTypes"]) ? d["Common.LeaseTypes"] : [],
    underOffer: d["Dynamic.UnderOffer"] === true,
    minAreaSqft: num(d["Dynamic.MinArea"]),
    maxAreaSqft: num(d["Dynamic.MaxArea"]),
    totalAreaSqft: num(d["Dynamic.TotalArea"]),
    totalBuildingSizeSqft: num(d["Dynamic.TotalBuildingSize"]),
    grossBuildingAreaSqft: num(d["Dynamic.GrossBuildingArea"]),
    grossLeasableAreaSqft: num(d["Dynamic.GrossLeasableArea"]),
    totalSizeAcres,
    minimumSizeAcres,
    minimumSizeSqft,
    parkingInternalSpaces: num(d["Common.InternalParkingSpaces"]),
    parkingExternalSpaces: num(d["Common.ExternalParkingSpaces"]),
    parkingRatio: num(parking["Common.Ratio"]),
    loadingDocks: num(d["Industrial.LoadingDocks"]),
    loadingDoors: num(d["Industrial.LoadingDoors"]),
    numberOfLots: num(d["Common.NumberOfLots"]),
    numberOfBedrooms: num(d["Common.NumberOfBedrooms"]),
    amenities,
    floorsAndUnits,
    demographics,
    transportation,
    energyPerformance: d["Common.EnergyPerformanceData"] ?? null,
    brokerIds,
    brochures,
    photos,
    floorPlans,
    externalBrochureUrl: absUrl(clean(d["Common.BrochureUrl"])),
    virtualTourUrl: absUrl(clean(d["Common.Walkthrough"])),
    propertyWebsiteUrl: absUrl(clean(d["Common.Website"])),
    aerialViewId: clean(d["Common.AerialViewID"]),
    otherUrls: Array.from(otherUrls),
    created: clean(d["Common.Created"]),
    lastUpdated: clean(d["Common.LastUpdated"])?.slice(0, 10) ?? null,
    childListingCount: num(d["Common.ListingCount"]),
  });
}

// ---------- main ----------

async function main() {
  const first = await fetchApiPage(1);
  const total: number = first.DocumentCount;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  console.error(`CBRE reports ${total} for-sale listings across ${totalPages} pages`);

  const seen = new Set<string>();
  let written = 0;

  process.stdout.write(
    `{"source":${JSON.stringify(SOURCE_URL)},` +
      `"sourceCompany":"CBRE","sourceSite":${JSON.stringify(SITE)},` +
      `"assetBaseUrl":${JSON.stringify(ASSET_BASE)},` +
      `"listingUrlTemplate":${JSON.stringify(DETAIL_BASE + "{id}/{slug}")},"defaultCountry":"US","defaultCurrency":"USD",` +
      `"listings":[`
  );

  const writeDocs = (docs: any[]) => {
    for (const d of docs ?? []) {
      const id = d["Common.PrimaryKey"];
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const chunk = JSON.stringify(toListing(d));
      process.stdout.write((written > 0 ? "," : "") + chunk);
      written++;
    }
  };

  writeDocs(first.Documents.flat());

  const remaining: number[] = [];
  for (let p = 2; p <= totalPages; p++) remaining.push(p);

  for (let i = 0; i < remaining.length; i += CONCURRENCY) {
    const batch = remaining.slice(i, i + CONCURRENCY);
    const results = await Promise.all(batch.map((p) => fetchApiPage(p)));
    for (const res of results) writeDocs(res.Documents.flat());
    console.error(`fetched pages ${batch.join(", ")} — ${written} listings so far`);
  }

  if (written === 0) {
    throw new Error("no listings returned from the CBRE listings API");
  }

  process.stdout.write(
    `],"brokers":${JSON.stringify(brokers.map(prune))},"totalListings":${written}}`
  );
  console.error(`collected ${written} unique listings and ${brokers.length} unique brokers`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
