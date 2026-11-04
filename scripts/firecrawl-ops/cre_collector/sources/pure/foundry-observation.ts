/** Foundry's legacy page-context envelope over shared bounded capture. */
import { captureSourcePageContext } from "../../lib/source-page-context/index.js";

export function captureFoundryPageContext(html: string, sourceUrl: string, detailObservedAt?: string) {
  return { ...captureSourcePageContext(html, { sourceUrl, detailObservedAt, descriptionSelector: ".property-description" }),
    schema: "foundry-native-page-context/v1" as const };
}
