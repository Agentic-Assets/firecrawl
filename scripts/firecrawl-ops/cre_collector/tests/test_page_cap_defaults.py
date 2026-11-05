"""CLI defaults are unlimited while explicit diagnostic caps are retained."""

import argparse

import pytest

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series


@pytest.mark.parametrize("module", [refresh, series])
@pytest.mark.parametrize(("args", "expected"), [([], 0), (["--page-cap", "3"], 3)])
def test_checkpoint_cli_page_cap(module, args, expected, monkeypatch):
    original = argparse.ArgumentParser.parse_args

    class Parsed(Exception):
        pass

    def capture(parser, argv=None, namespace=None):
        parsed = original(parser, argv, namespace)
        assert parsed.page_cap == expected
        raise Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", capture)
    with pytest.raises(Parsed):
        module.main(args)


def test_collector_command_default_and_explicit_cap(tmp_path):
    default = refresh.build_collect_argv("interra-realty", tmp_path / "out.json")
    finite = refresh.build_collect_argv("interra-realty", tmp_path / "out.json", page_cap=3)
    assert "--page-cap=0" in default
    assert "--page-cap=3" in finite
