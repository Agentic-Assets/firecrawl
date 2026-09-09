import assert from 'node:assert/strict';
import test from 'node:test';
import {
  BROWSER_BATCH_FETCH_MAX_REQUESTS,
  Semaphore,
  cleanupBrowserBatchResources,
  parseBrowserBatchFetchInput,
  withBrowserBatchHardTimeout,
} from './browser_batch_fetch';

function validInput() {
  return {
    bootstrapUrl: 'https://example.com/start',
    requests: [
      {
        url: 'https://example.com/api/search',
        method: 'POST',
        headers: {
          accept: 'application/json',
          'content-type': 'application/x-www-form-urlencoded',
        },
        body: 'q=office',
      },
    ],
  };
}

test('accepts a bounded same-origin POST batch', () => {
  const parsed = parseBrowserBatchFetchInput(validInput());
  assert.equal(parsed.bootstrapUrl, 'https://example.com/start');
  assert.equal(parsed.requests[0].url, 'https://example.com/api/search');
  assert.equal(parsed.timeoutMs, 30_000);
  assert.equal(parsed.waitAfterLoadMs, 0);
});

test('rejects credentials and cross-origin requests', () => {
  assert.throws(
    () => parseBrowserBatchFetchInput({ ...validInput(), bootstrapUrl: 'https://user:pass@example.com/' }),
    /must not contain credentials/,
  );
  const crossOrigin = validInput();
  crossOrigin.requests[0].url = 'https://other.example/api/search';
  assert.throws(() => parseBrowserBatchFetchInput(crossOrigin), /bootstrap origin/);
});

test('rejects methods, headers, and bodies outside the narrow contract', () => {
  const getRequest = validInput() as any;
  getRequest.requests[0].method = 'GET';
  assert.throws(() => parseBrowserBatchFetchInput(getRequest), /must be POST/);

  const authHeader = validInput() as any;
  authHeader.requests[0].headers.authorization = 'secret';
  assert.throws(() => parseBrowserBatchFetchInput(authHeader), /disallowed header authorization/);

  const oversizedBody = validInput();
  oversizedBody.requests[0].body = 'x'.repeat(64 * 1024 + 1);
  assert.throws(() => parseBrowserBatchFetchInput(oversizedBody), /per-request byte limit/);
});

test('rejects oversized batches and aggregate bodies', () => {
  const tooMany = validInput();
  tooMany.requests = Array.from(
    { length: BROWSER_BATCH_FETCH_MAX_REQUESTS + 1 },
    () => ({ ...validInput().requests[0] }),
  );
  assert.throws(() => parseBrowserBatchFetchInput(tooMany), /at most 16/);

  const aggregate = validInput();
  aggregate.requests = Array.from({ length: 9 }, (_, index) => ({
    ...validInput().requests[0],
    url: `https://example.com/api/search?page=${index}`,
    body: 'x'.repeat(64 * 1024),
  }));
  assert.throws(() => parseBrowserBatchFetchInput(aggregate), /aggregate byte limit/);
});

test('rejects invalid timeout bounds', () => {
  assert.throws(
    () => parseBrowserBatchFetchInput({ ...validInput(), timeoutMs: 999 }),
    /timeoutMs must be an integer/,
  );
  assert.throws(
    () => parseBrowserBatchFetchInput({ ...validInput(), waitAfterLoadMs: 30_001 }),
    /waitAfterLoadMs must be an integer/,
  );
});

test('rejects URL fragments and unsupported content types', () => {
  const fragmented = validInput();
  fragmented.requests[0].url += '#response-fragment';
  assert.throws(() => parseBrowserBatchFetchInput(fragmented), /must not contain a fragment/);

  const unsupported = validInput();
  unsupported.requests[0].headers['content-type'] = 'text/plain';
  assert.throws(() => parseBrowserBatchFetchInput(unsupported), /content-type is not allowed/);
});

test('hard timeout rejects a renderer operation that never settles', async () => {
  await assert.rejects(
    withBrowserBatchHardTimeout(new Promise(() => {}), 1, 'hard deadline'),
    /hard deadline/,
  );
});

test('hard timeout cleans up a resource that resolves after the caller timed out', async () => {
  let resolveOperation!: (value: string) => void;
  const operation = new Promise<string>((resolve) => { resolveOperation = resolve; });
  const cleaned: string[] = [];
  await assert.rejects(
    withBrowserBatchHardTimeout(
      operation,
      1,
      'hard deadline',
      (value) => { cleaned.push(value); },
    ),
    /hard deadline/,
  );
  resolveOperation('late-context');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(cleaned, ['late-context']);
});

test('bounded cleanup releases the shared permit after hung closes', async () => {
  let releases = 0;
  await cleanupBrowserBatchResources(
    () => new Promise(() => {}),
    () => new Promise(() => {}),
    () => { releases += 1; },
    1,
  );
  assert.equal(releases, 1);
});

test('timed-out semaphore waiter is removed and cannot consume a later permit', async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  await assert.rejects(semaphore.acquire(1), /acquisition exceeded/);
  assert.equal(semaphore.getQueueLength(), 0);

  semaphore.release();
  assert.equal(semaphore.getAvailablePermits(), 1);
  await semaphore.acquire(10);
  assert.equal(semaphore.getAvailablePermits(), 0);
  semaphore.release();
});
