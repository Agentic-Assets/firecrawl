"""Tests of the isolated source-literal parser, with no database access."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from available_space_count import parse_buildout_available_space_count


def test_source_literals_preserve_zero_and_integer_limit():
    assert parse_buildout_available_space_count("0 Spaces") == 0
    assert parse_buildout_available_space_count("1 Space") == 1
    assert parse_buildout_available_space_count("  23 Spaces  ") == 23
    assert parse_buildout_available_space_count("2147483647 Spaces") == 2147483647


def test_ambiguous_or_non_source_literals_are_not_inferred():
    for value in (
        None,
        1,
        True,
        "",
        "1",
        "-1 Spaces",
        "1–3 Spaces",
        "1 Space, 2 Spaces",
        "1 Space available",
        "1 parking space",
        "2147483648 Spaces",
        "9" * 100 + " Spaces",
        "١ Space",
    ):
        assert parse_buildout_available_space_count(value) is None, repr(value)
