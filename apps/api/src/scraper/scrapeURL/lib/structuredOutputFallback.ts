/**
 * Fork-owned (Agentic-Assets/firecrawl): structured-output validation and the
 * one-time fallback model. Upstream files only call this module's two hooks
 * (performSummary and extractData), so upstream syncs touch little fork code.
 *
 * With MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK set, a structured generation is
 * one transaction of at most two provider requests: the primary, run with
 * `boundedStructuredOutput` (no internal rate-limit retry or AI SDK repair),
 * then one request to the configured model, made only when the primary output
 * was missing, truncated, unparseable, or schema-invalid. Provider, auth,
 * policy, quota, and cost failures are never retried here.
 *
 * Callers pass generateCompletions in, so the caller (and its test mocks)
 * controls which implementation runs. This module still imports
 * normalizeJsonSchemaForModel from llmExtract.ts, which imports this module
 * back. That cycle is safe: each side only uses the other at call time, never
 * while the modules load.
 */
import { NoObjectGeneratedError } from "ai";
import Ajv from "ajv";
import { config } from "../../../config";
import { CostLimitExceededError } from "../../../lib/cost-tracking";
import { getModel } from "../../../lib/generic-ai";
import { TokenUsage } from "../../../controllers/v1/types";
import {
  normalizeJsonSchemaForModel,
  type generateCompletions,
  type GenerateCompletionsOptions,
} from "../transformers/llmExtract";

type Generate = typeof generateCompletions;
type Completion = Awaited<ReturnType<Generate>>;
type Attempt =
  | { ok: true; completion: Completion }
  | { ok: false; error: unknown };
type Outcome<Accepted> =
  | { status: "accepted"; completion: Completion; accepted: Accepted }
  | { status: "rejected"; primary: Attempt; fallback?: Attempt };

/** Truncated structured output (finishReason "length"); the fallback may retry it. */
export class StructuredOutputLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StructuredOutputLimitError";
  }
}

/**
 * Structured generation produced text that is missing, truncated, or cannot
 * be parsed or validated. It is an output compatibility failure, not a
 * provider request failure.
 */
function isInvalidStructuredOutputError(error: unknown): boolean {
  return (
    NoObjectGeneratedError.isInstance(error) ||
    error instanceof StructuredOutputLimitError
  );
}

async function attempt(completion: Promise<Completion>): Promise<Attempt> {
  try {
    return { ok: true, completion: await completion };
  } catch (error) {
    return { ok: false, error };
  }
}

/**
 * The single fallback policy. `accept` returns the usable payload of a
 * completion, or undefined when the output is unusable. Without a configured
 * fallback model this is exactly one ordinary generateCompletions call.
 */
async function runWithFallback<Accepted>(
  generate: Generate,
  options: GenerateCompletionsOptions,
  accept: (completion: Completion) => Accepted | undefined,
): Promise<Outcome<Accepted>> {
  const fallbackModelName =
    config.MODEL_NAME_STRUCTURED_OUTPUT_FALLBACK?.trim() || undefined;

  const primary = await attempt(
    generate({
      ...options,
      boundedStructuredOutput: fallbackModelName !== undefined,
    }),
  );
  const primaryAccepted = primary.ok ? accept(primary.completion) : undefined;
  if (primary.ok && primaryAccepted !== undefined) {
    return {
      status: "accepted",
      completion: primary.completion,
      accepted: primaryAccepted,
    };
  }

  if (
    fallbackModelName === undefined ||
    (!primary.ok && !isInvalidStructuredOutputError(primary.error))
  ) {
    return { status: "rejected", primary };
  }

  options.logger.warn(
    "Structured output missing or invalid; retrying once with configured fallback model",
    { fallbackModelName },
  );
  const fallback = await attempt(
    generate({
      ...options,
      model: getModel(fallbackModelName, "openai", {
        ignoreModelOverride: true,
      }),
      boundedStructuredOutput: true,
    }),
  );
  const fallbackAccepted = fallback.ok
    ? accept(fallback.completion)
    : undefined;
  if (fallback.ok && fallbackAccepted !== undefined) {
    options.logger.info("Structured-output fallback succeeded", {
      fallbackModelName,
    });
    return {
      status: "accepted",
      completion: fallback.completion,
      accepted: fallbackAccepted,
    };
  }
  return { status: "rejected", primary, fallback };
}

// ---------------------------------------------------------------------------
// Summary (performSummary hook)

function hasUsableSummary(extract: unknown): extract is { summary: string } {
  return (
    typeof extract === "object" &&
    extract !== null &&
    typeof (extract as { summary?: unknown }).summary === "string" &&
    (extract as { summary: string }).summary.trim().length > 0
  );
}

/**
 * Returns the deciding completion. Its extract holds a non-empty `summary`,
 * or is `{}` when no attempt produced one. Throws the deciding attempt's
 * error.
 */
export async function generateSummaryCompletion(
  generate: Generate,
  options: GenerateCompletionsOptions,
): Promise<Completion> {
  const outcome = await runWithFallback(generate, options, completion =>
    hasUsableSummary(completion.extract) ? completion.extract : undefined,
  );
  if (outcome.status === "accepted") {
    return outcome.completion;
  }

  const last = outcome.fallback ?? outcome.primary;
  if (!last.ok) {
    throw last.error;
  }
  options.logger.warn("LLM summary response did not include a usable summary", {
    model: last.completion.model,
  });
  return { ...last.completion, extract: {} };
}

// ---------------------------------------------------------------------------
// JSON extraction (extractData hook)

type ResolvedStructuredResult = {
  extractedData: unknown;
  wasDirectSchemaResult: boolean;
};

function isSmartScrapeEnvelope(value: unknown): value is {
  extractedData: unknown;
  shouldUseSmartscrape?: unknown;
} {
  return (
    typeof value === "object" &&
    value !== null &&
    Object.hasOwn(value, "extractedData")
  );
}

function schemaValidatedValue(
  value: unknown,
  schema: unknown,
): { value: unknown } | undefined {
  try {
    // Providers sometimes include an unrequested sibling field even when the
    // requested schema forbids it. Keep only schema-permitted output, then
    // validate required fields and types before accepting the result.
    const candidate =
      value === undefined ? undefined : JSON.parse(JSON.stringify(value));
    const validate = new Ajv({
      allErrors: false,
      removeAdditional: "failing",
      strict: false,
    }).compile(schema as any);
    return validate(candidate) ? { value: candidate } : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Some OpenAI-compatible providers return the requested user schema directly
 * instead of Firecrawl's SmartScrape envelope. Accept that response only when
 * it validates against the user schema; anything else is a failed structured
 * result and can use the configured fallback model.
 */
export function resolveStructuredResult(
  value: unknown,
  userSchema: unknown,
): ResolvedStructuredResult | undefined {
  // Some user schemas legitimately have an `extractedData` property at their
  // root. Validate the complete value first so that provider output matching
  // that schema is not mistaken for Firecrawl's internal envelope.
  const direct = schemaValidatedValue(value, userSchema);
  if (direct !== undefined) {
    return { extractedData: direct.value, wasDirectSchemaResult: true };
  }
  if (!isSmartScrapeEnvelope(value)) {
    return undefined;
  }

  const { shouldUseSmartscrape, extractedData } = value;
  if (
    shouldUseSmartscrape !== undefined &&
    typeof shouldUseSmartscrape !== "boolean"
  ) {
    return undefined;
  }
  if (shouldUseSmartscrape === true && extractedData === null) {
    return { extractedData, wasDirectSchemaResult: false };
  }
  const validated = schemaValidatedValue(extractedData, userSchema);
  return validated === undefined
    ? undefined
    : { extractedData: validated.value, wasDirectSchemaResult: false };
}

function failureWarning(prefix: string, error: unknown): string {
  const reason = error instanceof Error ? error.message : String(error);
  return `${prefix}: ${reason.slice(0, 300)}`;
}

/**
 * Drop-in for extractData's generateCompletions call. With a schema, the
 * returned extract carries only schema-valid data: the SmartScrape envelope
 * with a validated `extractedData` when `wrapForSmartScrape`, otherwise the
 * validated user data itself; undefined data when no attempt validated.
 * Errors that extractData's catch already handles (cost limit, a primary
 * failure that is not retried) are rethrown unchanged.
 */
export async function generateValidated(
  generate: Generate,
  wrapForSmartScrape: boolean,
  options: GenerateCompletionsOptions,
): Promise<{
  extract: any;
  warning: string | undefined;
  totalUsage: TokenUsage | undefined;
}> {
  // Validate against the provider-normalized schema actually sent.
  const normalized = normalizeJsonSchemaForModel(options.options.schema);
  const resultSchema = wrapForSmartScrape
    ? normalized?.properties?.extractedData
    : normalized;
  if (!resultSchema) {
    return generate(options);
  }
  // The rest of extractData reads the SmartScrape fields from the envelope
  // and the user data from `extractedData` (or, unwrapped, the extract).
  function withData(extract: any, data: unknown): any {
    if (!wrapForSmartScrape) {
      return data;
    }
    if (extract === undefined) {
      return undefined;
    }
    return { ...extract, extractedData: data };
  }

  const outcome = await runWithFallback(generate, options, completion =>
    resolveStructuredResult(completion.extract, resultSchema),
  );
  if (outcome.status === "accepted") {
    const { completion, accepted } = outcome;
    if (accepted.wasDirectSchemaResult) {
      options.logger.info("Normalized direct structured-output response");
    }
    return {
      ...completion,
      extract: withData(completion.extract, accepted.extractedData),
    };
  }

  const { primary, fallback } = outcome;
  if (!primary.ok && fallback === undefined) {
    throw primary.error;
  }
  if (
    fallback !== undefined &&
    !fallback.ok &&
    fallback.error instanceof CostLimitExceededError
  ) {
    throw fallback.error;
  }

  // A retried primary failure (for example the output-limit message) stays
  // actionable when the fallback then yields nothing usable.
  const primaryWarning = primary.ok
    ? primary.completion.warning
    : failureWarning("JSON extraction failed", primary.error);
  let warning = primaryWarning;
  if (fallback !== undefined) {
    const fallbackWarning = fallback.ok
      ? fallback.completion.warning
      : failureWarning("JSON extraction fallback failed", fallback.error);
    if (fallbackWarning) {
      warning = [primaryWarning, fallbackWarning].filter(Boolean).join(" ");
    }
  }
  return {
    extract: withData(
      primary.ok ? primary.completion.extract : undefined,
      undefined,
    ),
    warning,
    totalUsage: primary.ok ? primary.completion.totalUsage : undefined,
  };
}
