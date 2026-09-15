/**
 * Avison Young strict-detail is intentionally blocked here.
 *
 * The production adapter's member fidelity depends on browser-equivalent
 * public-detail rendering.  The existing direct path has retry/fallback
 * behavior, and this Wave must not silently treat its feed rows as equivalent
 * detail evidence.  A later source-owned producer may replace this blocker
 * only after an ephemeral one-attempt renderer is independently proven.
 */
import {
  C10ReceiptError,
  type PublicReceipt,
} from "../contracts.js";
import {
  type C10Member,
  type ReceiptProducer,
  type ReceiptProducerContext,
} from "../producer.js";

export const AVISON_YOUNG_C10_BLOCKER =
  "Avison Young strict-detail receipt is blocked: no offline-proven ephemeral one-attempt detail renderer";

export class AvisonYoungBlockedReceiptProducer implements ReceiptProducer {
  async produceEnumerationReceipt(_context: ReceiptProducerContext): Promise<PublicReceipt> {
    throw new C10ReceiptError(AVISON_YOUNG_C10_BLOCKER);
  }

  async produceMemberReceipt(
    _context: ReceiptProducerContext,
    _member: C10Member,
  ): Promise<PublicReceipt> {
    throw new C10ReceiptError(AVISON_YOUNG_C10_BLOCKER);
  }
}
