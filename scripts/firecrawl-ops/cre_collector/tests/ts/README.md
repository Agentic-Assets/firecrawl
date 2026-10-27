# TypeScript unit tests (`tests/ts/`)

Offline tests for collector TypeScript helpers, adapters and isolated runtime
integrations. No live providers, Firecrawl or Supabase; owned loopback fixtures
may simulate services.

Use a standalone no-hardlinks clone following the
[isolated verification recipe](../../SETUP.md#isolated-verification). See the
[test module contract](../CLAUDE.md) for transport isolation and fixture rules.

Run from `cre_collector/`:

```bash
npm run test:unit
```

Full check (typecheck + unit tests):

```bash
npm test
```

Uses Node's built-in `node:test` runner with the existing `tsx` devDependency.
Add files as `tests/ts/*.test.ts` (top-level) or `tests/ts/**/*.test.ts` (nested).
The npm script passes both globs so top-level smoke tests are included.

Source adapter tests must isolate argv before import (`process.argv = [process.argv[0]!, process.argv[1]!];`)
because `lib/config.ts` runs strict `parseArgs()` at load time.
