"""Backward-compatible Leibsohn page-observation projection entry point."""

from source_evidence.projections import buildout_page

VERSION = buildout_page.VERSIONS["leibsohn"]
MAP_VERSION = buildout_page.MAP_VERSIONS["leibsohn"]
MAX_BYTES = buildout_page.MAX_BYTES
MAX_RECEIPTS = buildout_page.MAX_RECEIPTS
METADATA_LABEL = buildout_page.METADATA_LABEL
METADATA_PATH = buildout_page.METADATA_PATH
PATHS = buildout_page.PATHS


def _require_source(source_key):
    if source_key != "leibsohn":
        raise ValueError("unsupported observation projection")


def project(document, *, source_key, numeric_literals=None):
    _require_source(source_key)
    return buildout_page.project(document, source_key=source_key, numeric_literals=numeric_literals)


def prepare(raw, *, source_key):
    _require_source(source_key)
    return buildout_page.prepare(raw, source_key=source_key)


def validate(document, receipt, *, source_key, numeric_literals=None):
    _require_source(source_key)
    return buildout_page.validate(document, receipt, source_key=source_key, numeric_literals=numeric_literals)
