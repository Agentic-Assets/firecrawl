import * as cheerio from "cheerio";

export interface AvisonLeaseFactsPageInput {
  sourceUrl: string;
  rawHtml: string;
}

export interface AvisonLeaseFactsInput {
  externalId: string;
  listingUrl: string;
  observedAt: string;
  pages: AvisonLeaseFactsPageInput[];
}

export interface AvisonLeaseFactObservation {
  suite_text: string | null;
  status_text: string | null;
  rent_text: string | null;
  area_text: string | null;
  lease_type_text: string | null;
  fields: Array<{ label: string; value: string | null }>;
  unparsed: string[];
}

export interface AvisonLeaseFacts {
  schema: "avison-lease-facts-v1";
  external_id: string;
  listing_url: string;
  pages: Array<{
    source_url: string;
    observed_at: string;
    role: "availability" | "overview";
    observations: AvisonLeaseFactObservation[];
  }>;
}

const AREA_LABELS = new Set(["area", "available size", "square footage", "total size"]);
const RENT_LABELS = new Set(["lease rate", "rent", "rental rate"]);
const SUITE_LABELS = new Set(["suite", "suite number", "unit"]);
const STATUS_LABELS = new Set(["availability", "status"]);
const LEASE_TYPE_LABELS = new Set(["lease type"]);
type CheerioSelection = ReturnType<ReturnType<typeof cheerio.load>>;

function textOf(selection: CheerioSelection): string | null {
  const text = selection.text().replace(/\s+/g, " ").trim();
  return text || null;
}

function labelKey(label: string): string {
  return label.toLowerCase().replace(/\s+/g, " ").trim();
}

function oneFieldValue(
  fields: Array<{ label: string; value: string | null }>,
  labels: Set<string>
): string | null {
  const values = fields
    .filter((field) => labels.has(labelKey(field.label)))
    .map((field) => field.value)
    .filter((value): value is string => value !== null);
  return values.length === 1 ? values[0]! : null;
}

function labeledLine(line: string): { label: string; value: string | null } | null {
  const match = /^([^:]{1,80}):\s*(.*)$/.exec(line);
  return match ? { label: match[1]!.trim(), value: match[2]!.trim() || null } : null;
}

function textValuesOf($: ReturnType<typeof cheerio.load>, selection: CheerioSelection): string[] {
  const values: string[] = [];
  selection.each((_index, element) => {
    const text = $(element).text().replace(/\s+/g, " ").trim();
    if (text) values.push(text);
  });
  return values;
}

function availabilityObservation(
  $: ReturnType<typeof cheerio.load>,
  container: ReturnType<ReturnType<typeof cheerio.load>>
): AvisonLeaseFactObservation {
  const fields: Array<{ label: string; value: string | null }> = [];
  const unparsed: string[] = [];
  container.find(".availability-card-info-item").each((_index, element) => {
    const item = $(element);
    const labels = textValuesOf($, item.find(".availability-card-info-item-title"));
    const values = textValuesOf($, item.find(".availability-card-info-item-value"));
    if (labels.length === 1) {
      if (values.length === 1) fields.push({ label: labels[0]!, value: values[0]! });
      else unparsed.push(...labels, ...values);
    } else {
      unparsed.push(...labels, ...values);
    }
  });

  const readSingle = (selector: string, role: string): string | null => {
    const values = textValuesOf($, container.find(selector));
    if (values.length > 1) unparsed.push(...values.map((value) => `${role}: ${value}`));
    return values.length === 1 ? values[0]! : null;
  };
  const suiteText = readSingle(".availability-card-name h3", "Suite");
  const statusText = readSingle(".availability-card-status-tag", "Status");
  const rentText = readSingle(".availability-card-rent h3", "Rent");
  return {
    suite_text: suiteText,
    status_text: statusText,
    rent_text: rentText,
    area_text: oneFieldValue(fields, AREA_LABELS),
    lease_type_text: oneFieldValue(fields, LEASE_TYPE_LABELS),
    fields,
    unparsed,
  };
}

function overviewObservation(
  $: ReturnType<typeof cheerio.load>,
  container: ReturnType<ReturnType<typeof cheerio.load>>
): AvisonLeaseFactObservation {
  const fields: Array<{ label: string; value: string | null }> = [];
  const unparsed: string[] = [];
  const listItems = container.find("li").toArray();
  const lines = listItems.length ? listItems : container.find("p").toArray();
  const texts = lines.map((element) => textOf($(element))).filter((line): line is string => line !== null);
  if (!texts.length) {
    const whole = textOf(container);
    if (whole) unparsed.push(whole);
  } else {
    for (const line of texts) {
      const field = labeledLine(line);
      if (field) fields.push(field);
      else unparsed.push(line);
    }
  }
  return {
    suite_text: oneFieldValue(fields, SUITE_LABELS),
    status_text: oneFieldValue(fields, STATUS_LABELS),
    rent_text: oneFieldValue(fields, RENT_LABELS),
    area_text: oneFieldValue(fields, AREA_LABELS),
    lease_type_text: oneFieldValue(fields, LEASE_TYPE_LABELS),
    fields,
    unparsed,
  };
}

export function extractAvisonLeaseFacts(
  input: AvisonLeaseFactsInput
): AvisonLeaseFacts | undefined {
  const externalId = input.externalId.trim();
  const listingUrl = input.listingUrl.trim();
  const observedAt = input.observedAt.trim();
  if (!externalId || !listingUrl || !observedAt) return undefined;

  const pages: AvisonLeaseFacts["pages"] = [];
  for (const page of input.pages) {
    if (!page.sourceUrl.trim() || typeof page.rawHtml !== "string") continue;
    const $ = cheerio.load(page.rawHtml);
    const cards = $(".availability-card-v2").toArray();
    if (cards.length) {
      pages.push({
        source_url: page.sourceUrl,
        observed_at: observedAt,
        role: "availability",
        observations: cards.map((card) =>
          availabilityObservation($, $(card).find(".availability-card-v2-container").first())
        ),
      });
    }
    const overviews = $(".building__overview-text").toArray();
    if (overviews.length) {
      pages.push({
        source_url: page.sourceUrl,
        observed_at: observedAt,
        role: "overview",
        observations: overviews.map((overview) => overviewObservation($, $(overview))),
      });
    }
  }
  if (!pages.length) return undefined;
  return {
    schema: "avison-lease-facts-v1",
    external_id: externalId,
    listing_url: listingUrl,
    pages,
  };
}
