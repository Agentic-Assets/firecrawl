"""Shared owner for newly introduced Firecrawl ops limits and budgets.

These benchmark-only constants do not alter the collector's existing limits.
"""
# Fixed cohort size for this paired experiment, including failed and unrun cells.
DOCUMENT_BENCH_MAX_DOCUMENTS = 30
# Configurable native-PDF trials may use larger explicitly selected corpora.
# This does not change the original experiment's fixed thirty-document contract.
NATIVE_PDF_BENCH_MAX_DOCUMENTS = 1000
NATIVE_PDF_BENCH_PROBE_MAX_OUTPUT_TOKENS = 4096
# Large original PDFs fit locally; a narrower provider limit remains a visible error.
DOCUMENT_BENCH_MAX_PDF_BYTES = 100 * 1024 * 1024
# Bound local text/reference allocation without silently slicing the document.
DOCUMENT_BENCH_MAX_TEXT_BYTES = 16 * 1024 * 1024
# Bound one API response; excess is an explicit failed outcome.
DOCUMENT_BENCH_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# Common generous ceiling below the tested providers' live output capabilities.
DOCUMENT_BENCH_MAX_OUTPUT_TOKENS = 65536
# Give long documents time; failures and uncertain billing remain recorded.
DOCUMENT_BENCH_TIMEOUT_SECONDS = 600
# Bound provider/host concurrency; the default leaves room for other work.
DOCUMENT_BENCH_MAX_WORKERS = 6
DOCUMENT_BENCH_DEFAULT_WORKERS = 4
# Absolute client budget override ceiling, with a smaller supervised-run default.
DOCUMENT_BENCH_BUDGET_USD = 75.0
DOCUMENT_BENCH_DEFAULT_BUDGET_USD = 50.0
# Planning hold exceeds catalog-based standard-tier input/output exposure here.
# Unknown/BYOK outcomes retain the hold; this is not a provider-enforced cap.
DOCUMENT_BENCH_CALL_RESERVATION_USD = 3.50
# Diagnostics should fit easily without retaining an unbounded provider error body.
DOCUMENT_BENCH_MAX_ERROR_BYTES = 64 * 1024
# Small private metadata inputs: credential references and qualification receipts.
DOCUMENT_BENCH_MAX_METADATA_BYTES = 1024 * 1024
# Filesystem-safe abstract identifiers, independent of source property names.
DOCUMENT_BENCH_MAX_ID_CHARS = 80
