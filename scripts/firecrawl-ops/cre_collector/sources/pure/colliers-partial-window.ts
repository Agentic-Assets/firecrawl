/** Select one bounded, deterministic rotating window; no write authority. */
import type { Readable } from "node:stream";
import { scanColliersPartial, type Candidate } from "./colliers-partial-spool.js";

// At most 4 MiB of serialized identity data, independent of source size.
const MAX_CANDIDATES = 2048;
// One bounded rotating window per source attempt; later runs resume at the
// committed cursor. This limits time spent before saving progress.
const DEFAULT_CANDIDATES = 100;
const MAX_IDENTITY_BYTES = 2048;

export async function selectColliersWindow(
  open: () => Readable, verify: () => Promise<void>,
  binding: { generation: string; attempt: number; startedAt: string; finishedAt: string },
  cursor: number, limit = DEFAULT_CANDIDATES,
) {
  if (!Number.isSafeInteger(cursor) || cursor < 1 || !Number.isSafeInteger(limit) || limit < 1 || limit > MAX_CANDIDATES)
    throw new Error("invalid partial window");
  const selected: Candidate[] = [];
  const position = (c: Candidate) => c.start + c.offset;
  const order = (a: Candidate, b: Candidate) =>
    Number(position(a) < cursor) - Number(position(b) < cursor) || position(a) - position(b);
  const scan = (visit: (card: Candidate, supported: boolean) => void,
                hole?: (start: number, size: number, reason: string) => void) =>
    scanColliersPartial(open(), binding.generation, binding.attempt, binding.startedAt, binding.finishedAt,
      undefined, visit, hole);
  const first = await scan((card, supported) => {
    if (!supported || Buffer.byteLength(JSON.stringify(card), "utf8") > MAX_IDENTITY_BYTES) return;
    // Repeated positions are counted in the second pass. Keep one
    // representative so the conflict is held without blocking other peers.
    if (selected.some(existing => position(existing) === position(card))) return;
    let low = 0, high = selected.length;
    while (low < high) {
      const mid = (low + high) >>> 1;
      if (order(selected[mid], card) <= 0) low = mid + 1;
      else high = mid;
    }
    if (low >= limit) return;
    selected.splice(low, 0, card);
    if (selected.length > limit) selected.pop();
  });
  await verify();
  // A page whose identities are unknown can conflict with any candidate.
  if (first.heldPages || first.stopped || (first.providerHoles < 1 && !first.countDrift) || !first.total)
    throw new Error("partial source window is not safely scoped");
  const counts = selected.map(() => ({ id: 0, url: 0, token: 0, position: 0, hole: false }));
  const second = await scan(card => {
    selected.forEach((candidate, index) => {
      const count = counts[index];
      if (card.mapProjectId === candidate.mapProjectId) count.id++;
      if (card.detailUrl === candidate.detailUrl) count.url++;
      if (card.detailPv === candidate.detailPv) count.token++;
      if (position(card) === position(candidate)) count.position++;
    });
  }, (start, size) => {
    selected.forEach((candidate, index) => {
      if (position(candidate) >= start && position(candidate) - start < size) counts[index].hole = true;
    });
  });
  await verify();
  if (second.heldPages || second.stopped || second.total !== first.total || second.available !== first.available ||
      second.indexed !== first.indexed || second.holes !== first.holes || second.providerHoles !== first.providerHoles ||
      second.countDrift !== first.countDrift)
    throw new Error("partial source changed between scans");
  const window = selected.map((candidate, index) => {
    const c = counts[index];
    return { candidate, eligible: c.id === 1 && c.url === 1 && c.token === 1 && c.position === 1 && !c.hole };
  });
  const eligible = window.filter(member => member.eligible).map(member => member.candidate);
  // The caller must derive a checkpoint cursor from the last actually
  // attempted member only after settlement; a full window can exceed its
  // artifact byte budget and contain unattempted members.
  return { eligible, window, selected: selected.length, heldSelected: selected.length - eligible.length,
    heldOther: first.indexed - selected.length, total: first.total, available: first.available, holes: first.holes };
}
