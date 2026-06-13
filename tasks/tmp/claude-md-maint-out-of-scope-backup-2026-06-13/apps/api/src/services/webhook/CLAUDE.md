# services/webhook/ -- webhook delivery

Handles outbound webhook delivery for all product features (crawl, scrape, batch, monitoring).

## Schema factory (schema.ts)

`createWebhookSchema(events)` -- the canonical way to define a webhook Zod schema for a set of events.

```ts
// in v2/types.ts
export const webhookSchema = createWebhookSchema(["completed", "failed", "page", "started"]);

// in monitoring/types.ts
const monitorWebhookSchema = createWebhookSchema(["monitor.page", "monitor.check.completed"]);
```

Always use this factory. It enforces the blacklisted-header check (`x-firecrawl-signature` is reserved for HMAC) and allows string shorthand (`"https://..."` is coerced to `{ url: "..." }`).

## Sender (delivery.ts)

`WebhookSender` class -- handles signing, filtering, and delivery.

- Created via `createWebhookSender(config)` from `index.ts`
- `sender.send(event, data)` -- filters by `config.events`, signs payload with HMAC-SHA256, delivers via undici
- Event matching: `"crawl.page"` matches filter entries `"crawl.page"`, `"page"`, or `"crawl"` (legacy subtype support)
- Large payloads are queued async via `webhookQueue` (BullMQ); small payloads may deliver inline

## Event naming convention

Events use `<namespace>.<type>` form. Examples: `crawl.page`, `crawl.completed`, `monitor.page`, `monitor.check.completed`. The `events` array in `webhookSchema` enumerates all valid events for a given product.

## Logs and DB

Webhook delivery attempts are logged to the DB via drizzle schema (`db/schema`). `logs.ts` provides query helpers. `config.ts` holds delivery retry settings.
