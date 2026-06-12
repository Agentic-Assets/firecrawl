# Colliers Scraper Notes

Colliers is currently unsupported in the production collector.

## Blocker

The property search at `https://www.colliers.com/en/properties` loads results through Coveo POST requests behind consent and application-gateway behavior. No stable public GET endpoint or server-rendered listing markup has been verified.

## Research Path

Use this folder for request-replay experiments, but keep production ingest disabled until a repeatable public path exists.
