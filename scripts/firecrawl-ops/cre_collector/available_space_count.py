"""Strict parsing of a single, already unambiguous Buildout availability fact.

This helper does not collect, mutate, store, or establish freshness of an
observation. Callers retain the original source text and resolve duplicate or
conflicting source observations before invoking it.
"""

from __future__ import annotations

import re

# PostgreSQL integer's inclusive upper bound; no arbitrary business maximum.
_INT32_MAX = 2_147_483_647
_BUILDOUT_SPACES = re.compile(r"([0-9]{1,10}) +Spaces?", re.ASCII)


def parse_buildout_available_space_count(value: object) -> int | None:
    """Parse '<integer> Space(s)' or return None without inventing a count.

    Zero is an explicit observation, not a missing value. This syntax check
    does not establish source identity, freshness, or field semantics.
    """
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _BUILDOUT_SPACES.fullmatch(value.strip())
    if match is None:
        return None
    count = int(match.group(1))
    return count if count <= _INT32_MAX else None
