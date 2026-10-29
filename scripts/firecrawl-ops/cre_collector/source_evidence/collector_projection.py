"""Dispatch explicitly selected source projections without changing legacy rules."""

from source_evidence import (
    buildout_projection,
    buildout_scope,
    map_projection,
)
from source_evidence.projections import buildout_enrichment, buildout_page


def supported(source_key, version):
    if not isinstance(source_key, str) or not isinstance(version, str):
        return False
    return ((version == buildout_enrichment.version_for(source_key) and version is not None)
            or (version == buildout_page.version_for(source_key) and version is not None)
            or version == map_projection.version_for(source_key)
            or (version == buildout_projection.VERSION and source_key in buildout_scope.supported_feed_keys()))


def prepare(raw, *, source_key, version):
    if not supported(source_key, version):
        raise ValueError("unsupported collector projection")
    if version == buildout_enrichment.version_for(source_key):
        return buildout_enrichment.prepare(raw, source_key=source_key)
    if version == buildout_page.version_for(source_key):
        return buildout_page.prepare(raw, source_key=source_key)
    if version == buildout_projection.VERSION:
        return buildout_projection.prepare(raw, source_key=source_key, representation="collector")
    return map_projection.prepare(raw, source_key=source_key)


def validate(document, receipt, *, source_key, numeric_literals=None):
    version = receipt.get("version") if isinstance(receipt, dict) else None
    if not supported(source_key, version):
        raise ValueError("unsupported collector projection")
    if version == buildout_enrichment.version_for(source_key):
        return buildout_enrichment.validate(document, receipt, source_key=source_key, numeric_literals=numeric_literals)
    if version == buildout_page.version_for(source_key):
        return buildout_page.validate(document, receipt, source_key=source_key, numeric_literals=numeric_literals)
    if version == buildout_projection.VERSION:
        return buildout_projection.validate(document, receipt, source_key=source_key,
                                           representation="collector", numeric_literals=numeric_literals)
    return map_projection.validate(document, receipt, source_key=source_key, numeric_literals=numeric_literals)


def canonical_digest(document, numeric_literals=None):
    return map_projection.canonical_digest(document, numeric_literals)
