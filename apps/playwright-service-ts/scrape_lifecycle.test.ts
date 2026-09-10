import assert from 'node:assert/strict';
import test from 'node:test';
import { Semaphore } from './browser_batch_fetch';
import { runScrapeLifecycle, ScrapeDeadlineError } from './scrape_lifecycle';
import { isInternalHost, TargetDnsUnavailableError } from './target_dns';
import { ScrapeStartPacer, scrapeStartIntervalMs } from './scrape_start_pacer';

const never = () => new Promise<never>(() => {});
function fixture(semaphore = new Semaphore(1)) {
  const calls: string[] = [];
  const page = { content: async () => '<html>property</html>' };
  const context = { newPage: async () => page };
  return {
    calls,
    page,
    context,
    options: {
      deadlineAt: Date.now() + 100,
      semaphore,
      prepare: async () => {},
      createContext: async () => {
        calls.push('context');
        return context;
      },
      createPage: (value: typeof context) => value.newPage(),
      work: (value: typeof page) => value.content(),
      closeContext: async () => {
        calls.push('close-context');
      },
      closePage: async () => {
        calls.push('close-page');
      },
      cleanupTimeoutMs: 5,
    },
  };
}

test('queued scrape expires without creating a browser context or stealing the next permit', async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  const { options, calls } = fixture(semaphore);
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(
    runScrapeLifecycle(options),
    (error: unknown) =>
      error instanceof ScrapeDeadlineError && error.phase === 'admission',
  );
  assert.equal(semaphore.getQueueLength(), 0);
  assert.deepEqual(calls, []);
  semaphore.release();
  assert.equal(
    await runScrapeLifecycle(fixture(semaphore).options),
    '<html>property</html>',
  );
  assert.equal(semaphore.getAvailablePermits(), 1);
});

for (const stage of ['newPage', 'content'] as const) {
  test(`hung ${stage} releases its permit and allows the next scrape`, async () => {
    const { options, context, page, calls } = fixture();
    options.deadlineAt = Date.now() + 10;
    if (stage === 'newPage') context.newPage = never;
    else page.content = never;
    await assert.rejects(
      runScrapeLifecycle(options),
      (error: unknown) =>
        error instanceof ScrapeDeadlineError && error.phase === 'work',
    );
    assert.ok(calls.includes('close-context'));
    assert.equal(options.semaphore.getAvailablePermits(), 1);
    assert.equal(
      await runScrapeLifecycle(fixture(options.semaphore).options),
      '<html>property</html>',
    );
  });
}

test('context resolving after timeout is closed once without releasing another permit', async () => {
  const { options, context, calls } = fixture();
  let resolveContext!: (value: typeof context) => void;
  options.createContext = () =>
    new Promise((resolve) => {
      resolveContext = resolve;
    });
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(runScrapeLifecycle(options), ScrapeDeadlineError);
  resolveContext(context);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, ['close-context']);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test('hung cleanup is bounded and successful content is preserved', async () => {
  const { options } = fixture();
  options.closeContext = never;
  options.closePage = never;
  assert.equal(await runScrapeLifecycle(options), '<html>property</html>');
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test('delivers success once before hanging cleanup while retaining the permit', async () => {
  const { options } = fixture();
  options.closePage = never;
  options.closeContext = never;
  options.cleanupTimeoutMs = 20;
  let deliveryCount = 0;
  let resolveDelivery!: (value: string) => void;
  const delivered = new Promise<string>((resolve) => {
    resolveDelivery = resolve;
  });
  let completed = false;
  const lifecycle = runScrapeLifecycle({
    ...options,
    deliverResult: (result) => {
      deliveryCount++;
      resolveDelivery(result);
    },
  }).then((result) => {
    completed = true;
    return result;
  });
  assert.equal(await delivered, '<html>property</html>');
  assert.equal(completed, false);
  assert.equal(options.semaphore.getAvailablePermits(), 0);
  assert.equal(await lifecycle, '<html>property</html>');
  assert.equal(options.semaphore.getAvailablePermits(), 1);
  assert.equal(deliveryCount, 1);
});

test('late page is closed without a second permit release', async () => {
  const { options, context, page, calls } = fixture();
  let resolvePage!: (value: typeof page) => void;
  context.newPage = () =>
    new Promise((resolve) => {
      resolvePage = resolve;
    });
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(runScrapeLifecycle(options), ScrapeDeadlineError);
  resolvePage(page);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, ['context', 'close-context', 'close-page']);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test('validation timeout does not enter browser admission', async () => {
  const { options, calls } = fixture();
  options.prepare = never;
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(
    runScrapeLifecycle(options),
    (error: unknown) =>
      error instanceof ScrapeDeadlineError && error.phase === 'admission',
  );
  assert.deepEqual(calls, []);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test('successful lifecycle returns the complete result without format changes', async () => {
  const { options } = fixture();
  const result = {
    content: '<html>rich page</html>',
    status: 200,
    contentType: 'text/html',
    headers: { 'content-type': 'text/html' },
  };
  assert.equal(
    await runScrapeLifecycle({ ...options, work: async () => result }),
    result,
  );
});

test('queue time consumes the work budget', async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  const { options } = fixture(semaphore);
  options.deadlineAt = Date.now() + 80;
  options.work = async () => {
    await new Promise((resolve) => setTimeout(resolve, 60));
    return 'late';
  };
  setTimeout(() => semaphore.release(), 40);
  await assert.rejects(
    runScrapeLifecycle(options),
    (error: unknown) =>
      error instanceof ScrapeDeadlineError && error.phase === 'work',
  );
  assert.equal(semaphore.getAvailablePermits(), 1);
});

test('DNS errors and empty resolution remain fail closed with a distinct transient error', async () => {
  await assert.rejects(
    isInternalHost('property.example', async () => {
      throw new Error('EAI_AGAIN');
    }),
    TargetDnsUnavailableError,
  );
  await assert.rejects(
    isInternalHost('property.example', async () => []),
    TargetDnsUnavailableError,
  );
});

test('private, mixed and public DNS answers preserve SSRF classification', async () => {
  assert.equal(await isInternalHost('127.0.0.1'), true);
  assert.equal(await isInternalHost('::1'), true);
  assert.equal(
    await isInternalHost('property.example', async () => [
      { address: '8.8.8.8' },
      { address: '10.0.0.1' },
    ]),
    true,
  );
  assert.equal(
    await isInternalHost('property.example', async () => [
      { address: '8.8.8.8' },
    ]),
    false,
  );
});

test('global pacing spaces concurrent context allocations after admission', async () => {
  const semaphore = new Semaphore(4);
  const pacer = new ScrapeStartPacer(15);
  const starts: number[] = [];
  await Promise.all(
    Array.from({ length: 4 }, () => {
      const { options, context } = fixture(semaphore);
      options.deadlineAt = Date.now() + 500;
      return runScrapeLifecycle({
        ...options,
        paceStart: (remaining) => {
          assert.ok(semaphore.getAvailablePermits() < 4);
          return pacer.waitForStart(remaining);
        },
        createContext: async () => {
          starts.push(performance.now());
          return context;
        },
      });
    }),
  );
  assert.equal(starts.length, 4);
  for (let index = 1; index < starts.length; index++) {
    assert.ok(
      starts[index] - starts[index - 1] >= 14,
      `start gap ${starts[index] - starts[index - 1]}ms`,
    );
  }
  assert.equal(semaphore.getAvailablePermits(), 4);
});

test('pacing deadline expires without context allocation or a late start', async () => {
  const pacer = new ScrapeStartPacer(60);
  await pacer.waitForStart(() => 100);
  const { options, calls } = fixture();
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(
    runScrapeLifecycle({
      ...options,
      paceStart: (remaining) => pacer.waitForStart(remaining),
    }),
    (error: unknown) =>
      error instanceof ScrapeDeadlineError && error.phase === 'admission',
  );
  assert.equal(options.semaphore.getAvailablePermits(), 1);
  await new Promise((resolve) => setTimeout(resolve, 70));
  assert.deepEqual(calls, []);
  // An idle gate has no reserved backlog: it must claim synchronously without
  // polling a second time, even when all previous callers have expired.
  let remainingChecks = 0;
  await pacer.waitForStart(() => {
    remainingChecks++;
    return 100;
  });
  assert.equal(remainingChecks, 1);
});

test('default zero pacing never schedules a delay; invalid intervals fail closed', async () => {
  assert.equal(scrapeStartIntervalMs(''), 0);
  assert.equal(scrapeStartIntervalMs('5000'), 5000);
  for (const value of ['-1', '5001', '1.5', 'invalid', 'Infinity']) {
    assert.throws(() => scrapeStartIntervalMs(value), /integer from 0 to 5000/);
  }
  const pacer = new ScrapeStartPacer(scrapeStartIntervalMs(''));
  let remainingChecks = 0;
  await Promise.all(
    Array.from({ length: 4 }, () =>
      pacer.waitForStart(() => {
        remainingChecks++;
        return 100;
      }),
    ),
  );
  assert.equal(remainingChecks, 4);
});
