import type { Tx } from "../../types.js";
/** Pure Matthews transaction classification. */
export function matthewsTenureFromUrl(url: string): Tx { return /\/properties\/leasing-/i.test(url) ? "lease" : "sale"; }
