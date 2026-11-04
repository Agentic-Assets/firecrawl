/** Fork-only opt-in for the shared no-auth team; hosted and preview limits stay separate. */
export function selfHostedConcurrencyLimit(
  useDbAuthentication: boolean | undefined,
  defaultLimit: number,
  rawValue = process.env.SELF_HOSTED_CONCURRENCY_LIMIT,
): number {
  if (useDbAuthentication !== false || rawValue === undefined) {
    return defaultLimit;
  }

  const value = rawValue.trim();
  if (!/^\d+$/.test(value)) return defaultLimit;
  const limit = Number(value);
  // Invalid configuration retains the conservative upstream default.
  return Number.isInteger(limit) && limit >= 1 && limit <= 32
    ? limit
    : defaultLimit;
}
