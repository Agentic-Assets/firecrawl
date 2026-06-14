# TypeScript unit tests (`tests/ts/`)

Pure unit tests for collector TypeScript helpers and adapters. No network, no
Firecrawl, no live Supabase.

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

**157 tests** across `lib/` and all 15 `sources/` adapters (pure helpers only).
Source adapter tests must isolate argv before import (`process.argv = [process.argv[0]!, process.argv[1]!];`)
because `lib/config.ts` runs strict `parseArgs()` at load time.
