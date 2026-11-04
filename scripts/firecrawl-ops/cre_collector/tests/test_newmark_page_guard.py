import pytest

from cre_newmark_page_guard import PREFIX, parse_output
from newmark_claim_witness import parse_page

ATTEMPT = "a" * 64


def test_exact_complete_outcome_required():
    for outcome in ("committed", "refused"):
        assert parse_output(PREFIX + ATTEMPT + " " + outcome + "\n", ATTEMPT) == outcome


@pytest.mark.parametrize("output", [
    "", PREFIX + ATTEMPT + " refused",
    PREFIX + ATTEMPT + " refused\n" + PREFIX + ATTEMPT + " refused\n",
    PREFIX + "b" * 64 + " refused\n",
    PREFIX + ATTEMPT + " unexpected\n",
])
def test_bad_outcome_remains_uncertain(output):
    with pytest.raises(ValueError):
        parse_output(output, ATTEMPT)


def test_page_witness_rejects_oversized_and_malformed():
    assert parse_page(None) is None
    assert parse_page("{}") is None
    assert parse_page("x" * 4097) is None
