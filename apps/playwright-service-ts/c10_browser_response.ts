/** Terminal response gate for the internal C10 browser route. */

export function c10TerminalEvidence(
  evidence: Record<string, unknown> | null,
  executionFailed: boolean,
  cleanupConfirmed: boolean,
): Record<string, unknown> | null {
  // The route must never serialize a signed success while a page/context/lease
  // might remain live. A caller receives only a generic quarantine failure.
  return !executionFailed && cleanupConfirmed && evidence ? evidence : null;
}
