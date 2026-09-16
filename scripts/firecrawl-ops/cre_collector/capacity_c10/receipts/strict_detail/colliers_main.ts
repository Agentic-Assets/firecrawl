/**
 * Colliers Main strict-detail is intentionally blocked here.
 *
 * Its public detail route requires browser/stealth challenge handling in the
 * production adapter.  The sealed C10 transport intentionally has no browser
 * fallback, retry, or cache mode, so emitting a lower-fidelity direct receipt
 * would be false evidence rather than a measurement.
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

export const COLLIERS_MAIN_C10_BLOCKER =
  "Colliers Main strict-detail receipt is blocked: no offline-proven ephemeral one-attempt browser evidence";

export class ColliersMainBlockedReceiptProducer implements ReceiptProducer {
  async produceEnumerationReceipt(_context: ReceiptProducerContext): Promise<PublicReceipt> {
    throw new C10ReceiptError(COLLIERS_MAIN_C10_BLOCKER);
  }

  async produceMemberReceipt(
    _context: ReceiptProducerContext,
    _member: C10Member,
  ): Promise<PublicReceipt> {
    throw new C10ReceiptError(COLLIERS_MAIN_C10_BLOCKER);
  }
}
