"""No-network guard for the duplicated TypeScript/Python source registries."""

import re
from collections import Counter
from pathlib import Path

import cre_ingest


COLLECTOR = Path(__file__).resolve().parent.parent
RECOVERED_BUILDOUT_SOURCE_KEYS = {
    "faris-lee",
    "fortis-net-lease",
    "unique-properties",
    "kiser-group",
    "pinnacle-rea",
    "cawley-chicago",
    "bradford-allen",
    "hudson-peters",
    "gibson-commercial",
    "leibsohn",
    "nai-hiffman",
    "nai-martens",
    "bull-realty",
    "tri-commercial",
    "berger-commercial",
    "nai-bergman",
    "nai-isaac",
    "trinity-partners",
    "metro-commercial",
    "33-realty",
    "nai-hallmark",
    "nai-plotkin",
    "greysteel",
    "nai-talcor",
    "nai-dominion",
}


def _typescript_source_keys():
    text = (COLLECTOR / "types.ts").read_text(encoding="utf-8")
    match = re.search(r"export const SOURCE_KEYS = \[(.*?)\] as const;", text, re.S)
    assert match, "SOURCE_KEYS tuple not found in types.ts"
    return re.findall(r'"([a-z0-9-]+)"', match.group(1))


def _registered_buildout_source_keys():
    text = (COLLECTOR / "sources" / "buildout-registry.ts").read_text(
        encoding="utf-8"
    )
    return re.findall(r'firm\(\s*"([a-z0-9-]+)"', text)


def _collector_switch_source_keys():
    text = (COLLECTOR / "collect.ts").read_text(encoding="utf-8")
    return set(re.findall(r'case "([a-z0-9-]+)":', text))


def _seeded_brokerage_slugs():
    text = (COLLECTOR.parent / "sql" / "001_cre_brokerages.sql").read_text(
        encoding="utf-8"
    )
    return set(re.findall(r"^\('[^']*(?:''[^']*)*', '([a-z0-9-]+)',", text, re.M))


def _seeded_brokerage_slug_counts():
    text = (COLLECTOR.parent / "sql" / "001_cre_brokerages.sql").read_text(
        encoding="utf-8"
    )
    return Counter(
        re.findall(r"^\('[^']*(?:''[^']*)*', '([a-z0-9-]+)',", text, re.M)
    )


def test_typescript_and_ingest_source_registries_are_exactly_equal():
    ts_keys = _typescript_source_keys()
    py_keys = list(cre_ingest.SOURCE_TO_BROKERAGE)
    assert ts_keys == py_keys, (
        "Source registry drift: update types.ts and cre_ingest.SOURCE_TO_BROKERAGE together. "
        f"TypeScript={ts_keys}; Python={py_keys}"
    )
    assert len(ts_keys) == 51


def test_buildout_sources_are_known_to_the_canonical_registry():
    assert cre_ingest.BUILDOUT_SOURCE_KEYS <= set(_typescript_source_keys())


def test_every_source_has_an_explicit_adapter_or_registered_buildout_fallback():
    assert set(_typescript_source_keys()) == (
        _collector_switch_source_keys() | set(_registered_buildout_source_keys())
    )


def test_recovered_buildout_registry_is_exactly_the_historical_25_firms():
    assert set(_registered_buildout_source_keys()) == RECOVERED_BUILDOUT_SOURCE_KEYS


def test_recovered_buildout_sources_share_strict_ingest_and_seed_contracts():
    assert (
        cre_ingest.BUILDOUT_SOURCE_KEYS
        == RECOVERED_BUILDOUT_SOURCE_KEYS
        | {"svn", "lee-associates", "franklin-street"}
    )
    assert RECOVERED_BUILDOUT_SOURCE_KEYS <= cre_ingest.STRICT_FRESHNESS_SOURCE_KEYS
    assert RECOVERED_BUILDOUT_SOURCE_KEYS <= (
        cre_ingest.CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS
    )
    assert RECOVERED_BUILDOUT_SOURCE_KEYS <= _seeded_brokerage_slugs()


def test_source_freshness_and_lifecycle_classes_are_exact():
    all_sources = set(cre_ingest.SOURCE_TO_BROKERAGE)
    assert cre_ingest.STRICT_FRESHNESS_SOURCE_KEYS == all_sources - {
        "cbre-dealflow",
        "colliers",
        "avison-young",
    }
    assert cre_ingest.AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS == (
        cre_ingest.BUILDOUT_SOURCE_KEYS
        | {
            "cbre",
            "cushman-wakefield",
            "srs",
            "hanley",
            "kidder-mathews",
            "newmark",
            "interra-realty",
        }
    )
    assert cre_ingest.CHILD_PRESERVING_AUTHORITATIVE_FEED_SOURCE_KEYS == (
        cre_ingest.AUTHORITATIVE_INVENTORY_FEED_SOURCE_KEYS - {"cbre"}
    )
    assert set(cre_ingest.STATUS_SOURCE_PATHS) == all_sources


def test_every_brokerage_mapping_has_one_sql_seed():
    expected_slugs = {slug for slug, _prefix in cre_ingest.SOURCE_TO_BROKERAGE.values()}
    counts = _seeded_brokerage_slug_counts()
    assert set(counts) == expected_slugs
    assert set(counts.values()) == {1}
