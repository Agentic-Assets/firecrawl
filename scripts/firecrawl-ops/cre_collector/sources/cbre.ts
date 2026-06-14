// sources/cbre.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { scrapeJson } from "../lib/scrape.js";
import { ScrapeOpts, SourceResult, Tx } from "../types.js";
import { clean, num, pmap } from "../lib/util.js";


// --- CBRE: internal listings JSON API, paginated, behind Cloudflare (stealth) ---

export async function srcCbre(tx: Tx, max: number, _monitor: boolean): Promise<SourceResult> {
  // Enumeration-only source: the listings-api JSON already returns fully mapped
  // rows with no per-listing detail render, so monitor output == full output.
  const aspect = tx === "sale" ? "isSale" : "isLetting";
  const opts: ScrapeOpts = { proxy: "stealth", waitFor: 4000, timeout: 120000 };
  const base = `https://www.cbre.com/listings-api/propertylistings/query?site=us-comm&Common.Aspects=${aspect}&PageSize=200`;
  const first = await scrapeJson(`${base}&Page=1`, opts);
  if (typeof first.DocumentCount !== "number" || !Array.isArray(first.Documents)) {
    throw new Error("CBRE listings API response is missing DocumentCount/Documents fields");
  }
  const total: number = first.DocumentCount;
  const want = Math.min(max, total);
  const pages = Math.ceil(want / 200);
  console.error(`  cbre/${tx}: ${total} total, fetching ${pages} page(s)`);
  const docsArr: any[][] = [first.Documents.flat()];
  if (pages > 1) {
    const pageNums = Array.from({ length: pages - 1 }, (_, i) => i + 2);
    const rest = await pmap(pageNums, CONCURRENCY, async (p) => {
      const d = await scrapeJson(`${base}&Page=${p}`, opts);
      console.error(`  cbre/${tx}: page ${p}/${pages} (${(d.Documents ?? []).flat().length} docs)`);
      return Array.isArray(d.Documents) ? d.Documents.flat() : [];
    });
    docsArr.push(...rest);
  }
  const docs = docsArr.flat().slice(0, want);
  const text = (loc: any) =>
    Array.isArray(loc) && loc.length ? clean(loc[0]["Common.Text"]) : null;
  const listings = docs.map((d: any) => {
    const addr = d["Common.ActualAddress"] ?? {};
    const charges: any[] = Array.isArray(d["Common.Charges"]) ? d["Common.Charges"] : [];
    const sale = charges.find(
      (c: any) => c["Common.ChargeKind"] === "SalePrice" && num(c["Common.Amount"])
    );
    const rent = charges.find(
      (c: any) => c["Common.ChargeKind"] === "Rent" && num(c["Common.Amount"])
    );
    const coord = d["Common.Coordinate"] ?? {};
    const aspects: string[] = Array.isArray(d["Common.Aspects"]) ? d["Common.Aspects"] : [];
    const name = clean(addr["Common.Line1"]);
    const street = clean(addr["Common.Line2"]);
    const city = clean(addr["Common.Locallity"]);
    const state = clean(addr["Common.Region"]);
    const zip = clean(addr["Common.PostCode"]);
    const slug = [name, street, city, state, zip]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
    const brokerIds = (Array.isArray(d["Common.Agents"]) ? d["Common.Agents"] : [])
      .map((a: any) =>
        brokerRef({
          name: clean(a["Common.AgentName"]),
          email: clean(a["Common.EmailAddress"]),
          phone: clean(a["Common.TelephoneNumber"]),
          office: clean(a["Common.AgentOffice"]),
          company: "CBRE",
        })
      )
      .filter((x: number | null): x is number => x !== null);
    const isSale = aspects.includes("isSale");
    const isLet = aspects.includes("isLetting");
    return {
      id: d["Common.PrimaryKey"],
      name,
      headline: text(d["Common.Strapline"]),
      transactionType: isSale && isLet ? "Sale/Lease" : isLet ? "Lease" : "Sale",
      assetType: clean(d["Common.UsageType"]),
      description: text(d["Common.LongDescription"]),
      street,
      city,
      state,
      postalCode: zip,
      country: clean(addr["Common.Country"]),
      latitude: typeof coord.lat === "number" ? coord.lat : null,
      longitude: typeof coord.lon === "number" ? coord.lon : null,
      salePriceUsd: sale ? sale["Common.Amount"] : null,
      salePriceText: sale || tx === "lease" ? null : "Contact broker for pricing",
      leaseRateText: rent
        ? `${rent["Common.Amount"]} ${clean(rent["Common.ChargeCurrency"]) ?? "USD"}/${clean(rent["Common.ChargeInterval"]) ?? ""} ${clean(rent["Common.ChargeBasis"]) ?? ""}`.trim()
        : null,
      buildingSizeSqft: num(d["Dynamic.TotalArea"]),
      brokerIds,
      brochures: (Array.isArray(d["Common.Brochures"]) ? d["Common.Brochures"] : []).map(
        (b: any) => ({
          name: clean(b["Common.BrochureName"]),
          url: clean(b["Common.Uri"])?.startsWith("http")
            ? clean(b["Common.Uri"])
            : `https://www.cbre.com${clean(b["Common.Uri"]) ?? ""}`,
        })
      ),
      photos: (Array.isArray(d["Common.Photos"]) ? d["Common.Photos"] : [])
        .map((p: any) => {
          const r =
            (p["Common.ImageResources"] ?? []).find(
              (x: any) => x["Common.Breakpoint"] === "original"
            ) ?? (p["Common.ImageResources"] ?? [])[0];
          const u = r && clean(r["Common.Resource.Uri"]);
          return u ? (u.startsWith("http") ? u : `https://www.cbre.com${u}`) : null;
        })
        .filter(Boolean),
      url: `https://www.cbre.com/properties/properties-for-lease/commercial-space/details/${d["Common.PrimaryKey"]}/${slug}`,
      lastUpdated: clean(d["Common.LastUpdated"])?.slice(0, 10) ?? null,
      created: clean(d["Common.Created"])?.slice(0, 10) ?? null,
    };
  });
  return {
    company: "CBRE",
    sourceUrl: `https://www.cbre.com/properties (${aspect})`,
    method: "CBRE public listings API (JSON, paginated, stealth proxy)",
    totalAvailable: total,
    listings,
  };
}
