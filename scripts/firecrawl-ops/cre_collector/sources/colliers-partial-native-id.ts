/** The Python handoff shares this exact native ProjectId contract. */
import { ColliersIdentityError } from "./colliers.js";

export function assertPartialNativeIdentity(detail: any, expected: string): void {
  const summary = detail?.ProjectSummary;
  const observed = summary?.AttributeVisibility?.ProjectId ?? summary?.ProjectId;
  if ((typeof observed !== "string" && !(typeof observed === "number" && Number.isSafeInteger(observed))) ||
      String(observed).trim() !== expected) throw new ColliersIdentityError("partial native ProjectId invalid");
}
