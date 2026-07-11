"""No-network guard for the duplicated TypeScript/Python source registries."""

import re
from pathlib import Path

import cre_ingest


COLLECTOR = Path(__file__).resolve().parent.parent


def _typescript_source_keys():
    text = (COLLECTOR / "types.ts").read_text(encoding="utf-8")
    match = re.search(r"export const SOURCE_KEYS = \[(.*?)\] as const;", text, re.S)
    assert match, "SOURCE_KEYS tuple not found in types.ts"
    return re.findall(r'"([a-z0-9-]+)"', match.group(1))


def test_typescript_and_ingest_source_registries_are_exactly_equal():
    ts_keys = _typescript_source_keys()
    py_keys = list(cre_ingest.SOURCE_TO_BROKERAGE)
    assert ts_keys == py_keys, (
        "Source registry drift: update types.ts and cre_ingest.SOURCE_TO_BROKERAGE together. "
        f"TypeScript={ts_keys}; Python={py_keys}"
    )


def test_buildout_sources_are_known_to_the_canonical_registry():
    assert cre_ingest.BUILDOUT_SOURCE_KEYS <= set(_typescript_source_keys())
