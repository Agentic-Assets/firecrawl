/** Source evidence, not comp qualification. Python mirror: cre_rent_evidence.py. */
export function rentEvidence(text: unknown, sourceFieldLabel: unknown = null) {
  const raw_quote = typeof text === "string" ? text : null;
  const source_field_label = typeof sourceFieldLabel === "string" ? sourceFieldLabel : null;
  const s = [raw_quote, source_field_label].filter(Boolean).join(" ");
  const monthly = /\/\s*(?:mo|month)\b|\bmonthly\b|\bper\s+month\b/i.test(s);
  const annual = /\/\s*(?:yr|year)\b|\bper\s+year\b|\bannual(?:ly)?\b/i.test(s);
  const original_period = monthly && annual ? "conflict" : monthly ? "monthly" : annual ? "annual" : "unknown";
  let denominator = /(?:\/|\bper\s+)\s*(?:sf\b|sq\.?\s*ft\b|square\s*(?:feet|foot))|\bpsf\b|\bsf\s*\/\s*(?:yr|mo|year|month)/i.test(s) ? "sf" : "unknown";
  if (/\b(?:sqm|m2|acres?|units?)\b|square\s*met(?:er|re)s?/i.test(s)) denominator = denominator === "sf" ? "conflict" : "unknown";
  const currencies = new Set(s.toUpperCase().match(/\b(?:USD|CAD|EUR|GBP|AUD)\b/g) ?? []);
  if (s.includes("€")) currencies.add("EUR");
  if (s.includes("£")) currencies.add("GBP");
  const currency = currencies.size === 1 ? [...currencies][0]! : currencies.size ? "conflict" : "unknown";
  let lease_basis: "modified_gross" | "full_service" | "nnn" | "gross" | null = null;
  if (/modified[ _-]gross|mod gross/i.test(s)) lease_basis = "modified_gross";
  else if (/full[ _-]service|\bfsg\b/i.test(s)) lease_basis = "full_service";
  else if (/\bnnn\b|triple[ -]net/i.test(s)) lease_basis = "nnn";
  else if (/\big\b|\bgross\b/i.test(s)) lease_basis = "gross";
  const basisConflict = /\bnnn\b|triple[ -]net/i.test(s) && /\bgross\b|\big\b|full[ _-]service|\bfsg\b/i.test(s);
  if (basisConflict) lease_basis = null;
  let clean = (raw_quote ?? "").replace(/,/g, "");
  const number = String.raw`(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)`;
  const money = clean.match(new RegExp(String.raw`(?:\bUSD|\bCAD|\bEUR|\bGBP|\bAUD|\$|€|£)\s*(${number})`, "i"));
  const trailing = clean.match(new RegExp(String.raw`(${number})\s*(?:USD|CAD|EUR|GBP|AUD)\b`, "i"));
  const selected = money ?? trailing;
  if (selected) clean = clean.slice(selected.index! + selected[0].indexOf(selected[1]!));
  const range = clean.trim().match(new RegExp(String.raw`^(${number})\s*(?:-|–|—|to)\s*\$?\s*(${number})`, "i"));
  const single = clean.trim().match(new RegExp(String.raw`^${number}(?=\s*(?:$|/|USD\b|CAD\b|EUR\b|GBP\b|AUD\b|SF\b|PSF\b|per\b|NNN\b|gross\b))`, "i"));
  const amounts = range ? [Number(range[1]), Number(range[2])] : single ? [Number(single[0])] : [];
  const anomalies: string[] = [];
  if (basisConflict) anomalies.push("lease_basis_conflict");
  for (const [key, value] of [["period", original_period], ["denominator", denominator], ["currency", currency]]) {
    if (value === "unknown" || value === "conflict") anomalies.push(`${key}_${value}`);
  }
  const valid = amounts.length > 0 && amounts.every(n => Number.isFinite(n) && n > 0 && Number.isFinite(n * (original_period === "monthly" ? 12 : 1) * 100)) && !/(?:^|USD\s*|\$\s*)-\s*\d/i.test(raw_quote ?? "");
  if (amounts.length && !valid) anomalies.push("invalid_amount");
  let annual_psf_min: number | null = null;
  let annual_psf_max: number | null = null;
  if (valid && ["annual", "monthly"].includes(original_period) && denominator === "sf" && currency === "USD") {
    const values = amounts.map(n => Math.round(n * (original_period === "monthly" ? 12 : 1) * 100) / 100);
    annual_psf_min = Math.min(...values);
    annual_psf_max = Math.max(...values) > annual_psf_min ? Math.max(...values) : null;
    if (Math.max(...values) > 500) anomalies.push("unusually_high_annual_psf");
    if (values.length > 1 && Math.max(...values) / Math.min(...values) >= 10) anomalies.push("wide_range");
  }
  return {raw_quote, source_field_label, original_period, denominator, currency, lease_basis, annual_psf_min, annual_psf_max, anomalies};
}
