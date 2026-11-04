"""Host-scoped document boilerplate, as a SQL predicate for the child-loss guards.

AGENTIC-5143 (W8). The collector stops attaching site boilerplate (Transwestern TREC
notices and service pages, JLL services pages, ...) as listing documents
(lib/document-boilerplate.ts). The rows already in ``cre_listing_documents`` are not
deleted by hand, so for the transition they must not count as listing documents in the
child-loss guard either: a full Transwestern ingest drops 8,012 of 11,234 documents, which
the 30% guard (``cre_ingest.CHILD_COUNT_RETAIN_*``) would otherwise treat as a parser
collapse. ``document_boilerplate_sql`` is excluded from the ``documents`` count on both
sides of the pre-commit guard (``cre_ingest.build_sql``) and of the post-commit comparison
(``cre_validate`` ``child_counts``), so the comparison stays symmetric and a collapse of
real documents still trips.

This is the host-scoped subset of the collector rule list; the any-host shapes (none today)
would not belong in a count exclusion. Hosts and path patterns are the strings in
``tests/fixtures/document_boilerplate_rules.json``, which ``lib/document-boilerplate.ts``,
this module and ``tests/run_document_boilerplate_guard_postgres_contract.py`` are all tested
against.

The SQL reads the URL as stored: a case-insensitive scheme, the lower-cased host (any trailing
dots allowed) and the raw path plus query, with no other normalization. It is not the
TypeScript filter, which normalizes first (repeated slashes, ``;params``, percent-encoded or
backslash separators, dot segments, a trailing slash, userinfo). So it matches a few shapes
the TypeScript keeps (an index URL such as ``/services/``, a dot segment under a denied prefix)
and misses spellings only the TypeScript resolves. The fixture's ``spellings`` record the
expected verdict of each side for every such URL, so any divergence is explicit and tested
on the TypeScript, Python and PostgreSQL sides. The corpus checked for AGENTIC-5143 holds only
lowercase ``https://``, single-slash, prefix-plus-file URLs for these hosts, which both sides
read the same way.

Pure and stdlib-only, so ``cre_ingest`` and ``cre_validate`` can both import it.
"""

import re
from typing import NamedTuple, Tuple


class BoilerplateRule(NamedTuple):
    name: str
    hosts: Tuple[str, ...]  # the exact host or any subdomain
    path_patterns: Tuple[str, ...]  # case-insensitive, JavaScript RegExp.source form; () = any path


DOCUMENT_BOILERPLATE_RULES: Tuple[BoilerplateRule, ...] = (
    BoilerplateRule(
        "transwestern_boilerplate",
        ("transwestern.com",),
        (r"^\/Upload\/TREC\/", r"^\/service\/", r"^\/services\/"),
    ),
    BoilerplateRule(
        "jll_services_boilerplate",
        ("jll.com",),
        (r"^\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?services?\/",),
    ),
    BoilerplateRule(
        "transwestern_privacy_policy",
        ("transwestern.com",),
        (r"(?:^|\/)privacy-policy\/?(?:\?|$)",),
    ),
    BoilerplateRule("transwestern_aetna_transparency", ("health1.aetna.com",), ()),
)

# Host (after any userinfo, before the port) and path plus query (no fragment) of the URL as stored.
# The scheme is matched case-insensitively with character classes, which mean the same in
# Python ``re`` and in PostgreSQL ARE (no embedded flag needed).
URL_HOST_RE = r"^[hH][tT][tT][pP][sS]?://(?:[^/?#@]*@)?([^/?#:]*)"
URL_PATH_RE = r"^[hH][tT][tT][pP][sS]?://[^/?#]*([^#]*)"


def host_regex(rule: BoilerplateRule) -> str:
    """Matches a lower-cased host equal to, or a subdomain of, one of the rule's hosts."""
    return r"(?:^|\.)(?:" + "|".join(re.escape(host) for host in rule.hosts) + r")\.*$"


def path_regex(rule: BoilerplateRule) -> str:
    """Matches a path plus query against any of the rule's patterns (case-insensitive)."""
    return "|".join(f"(?:{pattern})" for pattern in rule.path_patterns)


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def matches_boilerplate_url(url: str) -> bool:
    """The predicate's regexes run under ``re``: for tests that compare it with TypeScript and PostgreSQL."""
    host = re.match(URL_HOST_RE, url)
    path = re.match(URL_PATH_RE, url)
    if host is None or path is None:
        return False
    return any(
        re.search(host_regex(rule), host.group(1).lower())
        and (not rule.path_patterns or re.search(path_regex(rule), path.group(1), re.IGNORECASE))
        for rule in DOCUMENT_BOILERPLATE_RULES
    )


def document_boilerplate_sql(url_expr: str) -> str:
    """A NULL-safe boolean SQL expression: true when ``url_expr`` is a host-scoped boilerplate URL.

    Emitted only inside transactions that ``SET LOCAL standard_conforming_strings = on``
    (``cre_ingest.build_sql`` and both ``cre_validate`` read-only wrappers), so backslashes are literal.
    """
    host = f"lower(substring({url_expr} from {sql_literal(URL_HOST_RE)}))"
    path = f"substring({url_expr} from {sql_literal(URL_PATH_RE)})"
    clauses = []
    for rule in DOCUMENT_BOILERPLATE_RULES:
        clause = f"{host} ~ {sql_literal(host_regex(rule))}"
        if rule.path_patterns:
            clause += f" AND {path} ~* {sql_literal(path_regex(rule))}"
        clauses.append(f"({clause})")
    return "COALESCE(" + "\n      OR ".join(clauses) + ", false)"
