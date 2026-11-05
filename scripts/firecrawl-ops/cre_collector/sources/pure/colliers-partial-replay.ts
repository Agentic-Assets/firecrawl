/** Revalidate captured SalesTracker page pairs; not an admission decision. */
import * as cheerio from "cheerio";
import { groupColliersMapLocations, parseColliersReceiptCards } from "./colliers-receipt.js";

type Page = { start: number; pageSize: number; list: string; map: string; cards: Array<{ mapProjectId?: unknown; detailPv?: unknown; detailUrl?: unknown }> };

function count(value: unknown): number | null {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? value : null;
  if (typeof value !== "string" || !/^(0|[1-9][0-9]*)$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

/** The caller must bind the private spool, generation, source and attempt first. */
export function replayColliersPage(page: Page, expectedTotal: number, expectedAvailable: number) {
  if (!Number.isSafeInteger(page.start) || page.start < 1 || !Number.isSafeInteger(page.pageSize) || page.pageSize < 1 ||
      !Number.isSafeInteger(expectedTotal) || expectedTotal < page.start ||
      !Number.isSafeInteger(expectedAvailable) || expectedAvailable < expectedTotal ||
      page.pageSize > expectedTotal - page.start + 1 ||
      typeof page.list !== "string" || typeof page.map !== "string" || !Array.isArray(page.cards)) throw new Error("invalid page scope");
  const list = JSON.parse(page.list);
  const map = JSON.parse(page.map);
  if (!list || !map || list.success !== true || map.success !== true || typeof list.html !== "string" ||
      !Array.isArray(map.projectLocations) || count(list.total) !== expectedTotal || count(map.total) !== expectedTotal ||
      count(list.totalAvail) !== expectedAvailable || count(map.totalAvail) !== expectedAvailable) throw new Error("page response differs");
  const cardCount = cheerio.load(list.html)("li.item").length;
  const groups = groupColliersMapLocations(map.projectLocations);
  if (cardCount !== page.pageSize || count(list.numProjects) !== cardCount || groups.length !== cardCount) throw new Error("page count differs");
  const cards = parseColliersReceiptCards(list.html, groups, page.start);
  if (cards.length !== page.cards.length || cards.some((card, index) => {
    const captured = page.cards[index];
    return !captured || captured.mapProjectId !== card.mapProjectId || (captured.detailPv ?? null) !== card.detailPv || (captured.detailUrl ?? null) !== card.detailUrl;
  })) throw new Error("captured card identity differs");
  return cards;
}
