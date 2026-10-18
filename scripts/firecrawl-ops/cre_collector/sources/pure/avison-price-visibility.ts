/** Avison-owned asking visibility. Independent rents and nonprice facts survive. */
export type AvisonAskingControl = "absent" | "visible" | "withheld";
const SCHEMA = "avison-asking-price-v1";
const object = (v: any): v is Record<string, any> => v !== null && typeof v === "object" && !Array.isArray(v);
const words = (key: string) => key.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
const compact = (key: string) => words(key).join("");
const ASK_KEYS = new Set(["salepriceusd", "salepricetext", "salepricepersf", "saleunitprice", "saleprice", "askingprice", "askingamount"]);
const GENERIC_PRICE = new Set(["price", "amount", "value", "priceperunit", "priceperacre"]);
const PROSE = new Set(["description", "markdown", "highlights", "amenities", "extrafacts", "note", "notes", "caption", "label", "title", "hidepricetext"]);
const INDEPENDENT = new Set(["lease", "rent", "leaseratetext", "leaseratemin", "leaseratemax", "leaseratetype", "availabilitiesminrent", "availabilitiesmaxrent", "caprate", "capratepct", "noi", "netoperatingincome", "grm", "grossrentmult", "grossrentmultiplier", "grossrevenue", "revpar", "expenses", "operatingexpenses", "tax", "taxes"]);
const IDENTITY = new Set(["id", "externalid", "sourcekey", "brokerageid", "address", "street", "city", "state", "statelong", "zip", "postalcode", "country", "location", "latitude", "longitude", "lat", "lng", "phone", "email", "license", "licensenumber", "url", "href", "src", "externalurl", "sharplaunchurl", "canonicalurl", "profileurl", "vcardurl", "imagepath", "generationid", "refreshgenerationid", "generationstartedat", "startedat", "finishedat", "inventoryobservedat", "detailobservedat", "bodysha256", "responsetextsha256"]);
const ASK_LABEL = /\b(?:asking(?:\s+price)?|sale\s+price|purchase\s+price|for\s+sale\s+at|offered\s+at)\b/i;
const INDEPENDENT_LABEL = /\b(?:rent|lease\s+rate|NOI|net\s+operating\s+income|cap\s+rate|GRM|tax(?:es)?|operating\s+expenses)\b/i;
const LABEL_BOUNDARY = /(?:\s+(?:and|-)\s+)?(?=\b(?:asking(?:\s+price)?|sale\s+price|purchase\s+price|for\s+sale\s+at|offered\s+at|rent|lease\s+rate|NOI|net\s+operating\s+income|cap\s+rate|GRM|tax(?:es)?|operating\s+expenses)\b)/i;
const LEASE_BASIS = /\/\s*SF\s*\/\s*(?:YR|MO)\b/i;
const MONEY = /[$€£¥]\s*\d|\b(?:USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF)\s*\d|\b\d[\d,.]*\s*(?:USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF|dollars?)\b|\b\d[\d,.]*\s*(?:million|thousand)\b(?!\s*(?:SF\b|sq\.?\s*(?:ft|feet|meters?)\b|acres?\b|ac\b|ft\b|feet\b|years?\b|(?:parking\s+)?spaces?\b|units?\b|rooms?\b|doors?\b))/i;

export function avisonAskingControl(feed: any): AvisonAskingControl {
  if (!object(feed)) throw new Error("Avison asking control requires a feed object");
  const aliases = Object.keys(feed).filter(k => compact(k) === "hideprice");
  if (!aliases.length) return "absent";
  if (aliases.length !== 1 || aliases[0] !== "hide_price") throw new Error("Avison asking control key is ambiguous");
  if (feed.hide_price === 0 && typeof feed.hide_price === "number") return "visible";
  if (feed.hide_price === 1 && typeof feed.hide_price === "number") return "withheld";
  throw new Error("Avison asking control must be numeric zero or one");
}

function knownAskingNumber(text: string, amounts: readonly number[]): boolean {
  // Exclude protected spans; never perform equality matching on resource/identity fields.
  const prose = text.replace(/https?:\/\/[^\s)]+|[\w.+-]+@[\w.-]+|\b\d{3}[-.]\d{3}[-.]\d{4}\b/g, " ");
  for (const match of prose.matchAll(/\b\d[\d,]*(?:\.\d+)?(?:\s*(?:million|thousand|[KM])\b)?/gi)) {
    const token = match[0];
    const after = prose.slice(match.index! + token.length);
    const before = prose.slice(0, match.index);
    if (/^\s*(?:%|SF\b|sq\.?\s*(?:ft|feet|meters?)\b|acres?\b|ac\b|ft\b|feet\b|years?\b|(?:parking\s+)?spaces?\b|units?\b|rooms?\b|doors?\b)/i.test(after)) continue;
    if (/^\s+\w+(?:\s+\w+)?\s+(?:St(?:reet)?|Rd|Road|Ave(?:nue)?|Blvd|Drive|Dr)\b/i.test(after)) continue;
    if (/\b(?:built|since|year|replaced)\s*$/i.test(before)) continue;
    const number = Number(token.replace(/,/g, "").replace(/\s*(million|thousand|[KM])$/i, ""));
    const scale = /(?:million|M)$/i.test(token) ? 1e6 : /(?:thousand|K)$/i.test(token) ? 1e3 : 1;
    if (amounts.some(amount => Math.abs(amount - number * scale) < 0.000001)) return true;
  }
  return false;
}

export function sanitizeAvisonAskingText(text: string, amounts: readonly number[] = [], role = "narrative"): string {
  if (role === "identity") return text;
  // Markdown destinations are resources, not prose. Sanitize labels separately.
  const links: string[] = [];
  const input = text.replace(/\[([^\]]*)\]\(([^)]+)\)/g, (_, label, url) => {
    const safe = sanitizeAvisonAskingText(label, amounts, "child-title");
    links.push(`[${safe}](${url})`);
    return `AVISONLINK${links.length - 1}TOKEN`;
  }).replace(/https?:\/\/[^\s)]+|[\w.+-]+@[\w.-]+|\b\d{3}[-.]\d{3}[-.]\d{4}\b/g, span => {
    links.push(span);
    return `AVISONLINK${links.length - 1}TOKEN`;
  });
  const result: string[] = [];
  for (const clause of input.split(/(?<=[.!?;])\s+|[\r\n]+/)) {
    for (const part of clause.split(LABEL_BOUNDARY)) {
      const segment = part.trim().replace(/(?:\s+(?:and|-))\s*$/i, "");
      if (!segment) continue;
      // Basis protection is local to this segment, after splitting mixed labels.
      const explicitAsking = /\b(?:asking(?:\s+price)?|sale\s+price|purchase\s+price|for\s+sale\s+at)\b/i.test(segment);
      const independent = !explicitAsking && (LEASE_BASIS.test(segment) || INDEPENDENT_LABEL.test(segment))
        || role === "independent" && !ASK_LABEL.test(segment);
      if (!independent && (ASK_LABEL.test(segment) || MONEY.test(segment)
          || role !== "identity-caption" && knownAskingNumber(segment, amounts))) continue;
      result.push(segment);
    }
  }
  return result.join(" ").replace(/[;\s]+$/, "").replace(/AVISONLINK(\d+)TOKEN/g, (_, i) => links[Number(i)]!);
}

type Context = "" | "asking" | "independent" | "pricing" | "identity";
function contextOf(value: Record<string, any>, inherited: Context): Context {
  const business = String(value.businessFunction ?? "").toLowerCase();
  if (business === "lease" || business === "rent") return "independent";
  if (["sell", "sale"].includes(business)) return "asking";
  if (business.includes("lease") && business.includes("sale")) return "pricing";
  if (inherited !== "independent" && typeof value.note === "string" && /\basking\b/i.test(value.note)) return "asking";
  return inherited;
}

const OTHER_LABEL = /\b(?:rent|rental\s+rate|lease\s+rate|noi|net\s+operating\s+income|cap\s+rate|grm|tax(?:es)?|operating\s+expenses)\b/i;
const LEASE_FACT_TEXT_FIELDS = new Set([
  "suite_text", "status_text", "rent_text", "area_text", "lease_type_text",
]);

function projectAvisonLeaseField(field: any, amounts: readonly number[]): any {
  if (!object(field) || typeof field.label !== "string") return field;
  const label = field.label;
  const value = field.value;
  if (value !== null && typeof value !== "string") return field;
  if (ASK_LABEL.test(label)) return undefined;
  if (OTHER_LABEL.test(label)) {
    if (typeof value === "string" && ASK_LABEL.test(value)) return undefined;
  } else if (value !== null && value !== undefined) {
    const text = String(value);
    if (sanitizeAvisonAskingText(text, amounts, "narrative") !== text) return undefined;
  }
  return field;
}

function projectAvisonLeaseObservation(observation: any, amounts: readonly number[]): any {
  if (!object(observation)) return undefined;
  const { fields, ...rest } = observation;
  const prepared = { ...rest };
  for (const key of LEASE_FACT_TEXT_FIELDS) {
    if (typeof prepared[key] !== "string") continue;
    if (ASK_LABEL.test(prepared[key])) {
      prepared[key] = null;
      continue;
    }
    const role = key === "rent_text"
      ? "independent"
      : key === "suite_text" || key === "area_text" ? "identity-caption" : "narrative";
    const sanitized = sanitizeAvisonAskingText(prepared[key], amounts, role);
    if (sanitized !== prepared[key]) prepared[key] = null;
  }
  if (Array.isArray(prepared.unparsed)) {
    prepared.unparsed = prepared.unparsed.map((text: any) => {
      if (typeof text !== "string" || ASK_LABEL.test(text)) return null;
      const sanitized = sanitizeAvisonAskingText(text, amounts, "narrative");
      return sanitized || null;
    }).filter((text: any) => text !== null);
  }
  const projected = projectAvisonAskingValue(prepared, amounts, "independent");
  if (Array.isArray(fields)) {
    projected.fields = fields.map((field) => projectAvisonLeaseField(field, amounts))
      .filter((field) => field !== undefined);
  }
  return projected;
}

function projectAvisonLeasePage(page: any, amounts: readonly number[]): any {
  if (!object(page) || !Array.isArray(page.observations)) {
    return projectAvisonAskingValue(page, amounts, "independent");
  }
  const { observations, ...rest } = page;
  return {
    ...projectAvisonAskingValue(rest, amounts, "independent"),
    observations: observations.map((observation: any) =>
      projectAvisonLeaseObservation(observation, amounts)
    ).filter((observation: any) => observation !== undefined),
  };
}

function projectAvisonLeaseFacts(value: any, amounts: readonly number[]): any {
  if (!object(value) || value.schema !== "avison-lease-facts-v1" || !Array.isArray(value.pages)) {
    return projectAvisonAskingValue(value, amounts, "independent");
  }
  const { pages, ...rest } = value;
  return {
    ...projectAvisonAskingValue(rest, amounts, "independent"),
    pages: pages.map((page: any) => projectAvisonLeasePage(page, amounts))
      .filter((page: any) => page !== undefined),
  };
}

export function projectAvisonAskingValue(value: any, amounts: readonly number[] = [], context: Context = ""): any {
  if (context === "identity") return value;
  if (Array.isArray(value)) return value.map(v => projectAvisonAskingValue(v, amounts, context)).filter(v => v !== undefined && v !== "");
  if (typeof value === "string") {
    if (/^https?:\/\//i.test(value)) return value;
    return sanitizeAvisonAskingText(value, amounts, context === "independent" ? "independent" : "narrative");
  }
  if (!object(value)) return value;
  const active = contextOf(value, context);
  const out: Record<string, any> = {};
  for (const [key, item] of Object.entries(value)) {
    const normalized = compact(key), tokens = words(key);
    if (IDENTITY.has(normalized) || ["url", "href", "src"].includes(tokens.at(-1) ?? "") && typeof item === "string" && /^https?:\/\//i.test(item)) { out[key] = item; continue; }
    if (normalized === "avisonleasefacts") {
      out[key] = projectAvisonLeaseFacts(item, amounts);
      continue;
    }
    if (ASK_KEYS.has(normalized) || tokens.includes("asking") || tokens.includes("sale") && (tokens.includes("price") || tokens.includes("amount"))) {
      if (object(item) || Array.isArray(item)) out[key] = projectAvisonAskingValue(item, amounts, "asking");
      continue;
    }
    if (normalized === "hidepricetext" && typeof item !== "string") continue;
    if (normalized === "value" && typeof value.unit === "string" && value.unit.trim() && !/[$€£]|USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF|dollars?|euros?|pounds?|price|money/i.test(value.unit)) { out[key] = item; continue; }
    if (GENERIC_PRICE.has(normalized) && !object(item) && !Array.isArray(item)) {
      if (active === "asking") continue;
      if (active !== "independent" && (active === "pricing" || !["amount", "value"].includes(normalized))) throw new Error("Avison ambiguous financial input");
    }
    if (normalized === "avisonpricevisibility") {
      if (!object(item) || Object.keys(item).sort().join(",") !== "control,schema"
          || item.schema !== SCHEMA || !["absent", "visible", "withheld"].includes(item.control)) throw new Error("Avison asking marker is invalid");
      out[key] = item; continue;
    }
    if (IDENTITY.has(normalized)) { out[key] = item; continue; }
    let next: Context = active;
    if (INDEPENDENT.has(normalized) || tokens.includes("lease") || tokens.includes("rent")) next = "independent";
    else if (normalized === "pricing" || normalized === "pricespecification" || normalized === "offers") next = "pricing";
    else if (normalized === "sale" || normalized === "saleoffer") next = "asking";
    let projected: any;
    if (typeof item === "string" && INDEPENDENT.has(normalized) && !PROSE.has(normalized)) {
      projected = sanitizeAvisonAskingText(item, amounts, "independent");
    } else if (typeof item === "string" && ["name", "headline", "metatitle"].includes(normalized)) {
      projected = sanitizeAvisonAskingText(item, amounts, "identity-caption");
    } else if (typeof item === "string" && !PROSE.has(normalized) && next === "" && !ASK_LABEL.test(item) && !MONEY.test(item)) {
      projected = item; // A new harmless string is not automatically narrative or financial.
    } else projected = projectAvisonAskingValue(item, amounts, next);
    if (projected !== undefined && projected !== "") out[key] = projected;
  }
  return out;
}

export function projectAvisonAskingPrice(listing: any): any {
  if (!object(listing) || listing.sourceKey !== "avison-young") throw new Error("Avison asking projection requires the Avison source");
  const amounts: number[] = [];
  const visit = (node: any): AvisonAskingControl => {
    if (!object(node)) return "absent";
    const controls: AvisonAskingControl[] = [];
    if (node.sourceKey !== undefined && node.sourceKey !== "avison-young") throw new Error("Avison asking source conflict");
    if (object(node.rawSharpLaunch)) {
      controls.push(avisonAskingControl(node.rawSharpLaunch));
    }
    for (const [key, val] of Object.entries(node)) {
      if (ASK_KEYS.has(compact(key))) {
        const number = typeof val === "number" ? val : typeof val === "string" ? Number(val.replace(/[$,]/g, "")) : NaN;
        if (Number.isFinite(number) && number > 0) amounts.push(number);
      }
    }
    if (object(node.rawSharpLaunch)) {
      for (const key of ["sale_price", "sale_unit_price"]) {
        const number = Number(node.rawSharpLaunch[key]);
        if (Number.isFinite(number) && number > 0) amounts.push(number);
      }
    }
    for (const key of ["primary", "secondary_pass", "latestInventoryObservation"]) controls.push(visit(node[key]));
    const control: AvisonAskingControl = controls.includes("withheld") ? "withheld" : controls.includes("visible") ? "visible" : "absent";
    if (Object.hasOwn(node, "avisonPriceVisibility")) {
      const ownControl = object(node.rawSharpLaunch) ? avisonAskingControl(node.rawSharpLaunch) : control;
      // A saved current-observation wrapper may already carry the effective marker.
      const currentWrapper = object(node.latestInventoryObservation) && control === "withheld";
      const marker = node.avisonPriceVisibility;
      if (!object(marker) || Object.keys(marker).sort().join(",") !== "control,schema"
          || marker.schema !== SCHEMA || !(marker.control === ownControl || currentWrapper && marker.control === control)) throw new Error("Avison asking marker contradicts raw control");
    }
    return control;
  };
  const control = visit(listing);
  const projected = control === "withheld" ? projectAvisonAskingValue(listing, amounts) : { ...listing };
  return { ...projected, avisonPriceVisibility: { schema: SCHEMA, control } };
}
