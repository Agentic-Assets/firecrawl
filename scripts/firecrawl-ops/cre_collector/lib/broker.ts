// lib/broker.ts - extracted verbatim from collect.ts (see tasks/tmp backup)


// ---------- broker dedupe table (shared across sources) ----------

export const brokerIndex = new Map<string, number>();
export const brokers: any[] = [];

export function brokerRef(b: {
  name: string | null;
  email?: string | null;
  phone?: string | null;
  office?: string | null;
  avatarUrl?: string | null;
  company: string;
}): number | null {
  if (!b.name && !b.email) return null;
  const key = (b.email ?? "") + "|" + (b.name ?? "") + "|" + b.company;
  const existing = brokerIndex.get(key);
  if (existing !== undefined) {
    const rec = brokers[existing];
    if (!rec.phone && b.phone) rec.phone = b.phone;
    if (!rec.office && b.office) rec.office = b.office;
    if (!rec.avatarUrl && b.avatarUrl) rec.avatarUrl = b.avatarUrl;
    return existing;
  }
  const idx = brokers.length;
  brokers.push({
    name: b.name ?? null,
    email: b.email ?? null,
    phone: b.phone ?? null,
    office: b.office ?? null,
    avatarUrl: b.avatarUrl ?? null,
    company: b.company,
  });
  brokerIndex.set(key, idx);
  return idx;
}
